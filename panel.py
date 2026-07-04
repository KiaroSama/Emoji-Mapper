"""Local web panel to review/curate downloaded emoji before publishing.

Opens a dark neon-blue panel in the browser showing every emoji in the catalog
as a large card with a label. All are selected (included) by default; click a
card to toggle it (deselected = excluded from the next publish). Visually
similar emoji are ordered next to each other (greedy nearest-neighbour on the
perceptual hash) so you can deselect look-alikes quickly. "Save" writes the
selection back to the catalog; build_collection then only publishes included
items.

Run:  python panel.py            (serves http://127.0.0.1:8765 and opens it)
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import logging
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from build_collection import BRAND_LOGO_BOTS, BRAND_LOGO_DEFAULT
from emojikit.catalog import Catalog
from emojikit.logsetup import setup_logging
from emojikit.media import hamming

ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets" / "vendor"
log = logging.getLogger("panel")

_MIME = {".webp": "image/webp", ".png": "image/png", ".gif": "image/gif",
         ".webm": "video/webm", ".tgs": "application/gzip"}
FMT_ORDER = {"static": 0, "video": 1, "animated": 2}
LOGO_KEY = "__brand_logo__"  # pseudo content_key: preview-only, never saved/counted


def order_by_similarity(items: list) -> list:
    """Greedy nearest-neighbour ordering by perceptual hash, grouped by format.

    Items without a perceptual hash (e.g. animated .tgs) keep content order and
    follow the hashed ones within their format group.
    """
    out: list = []
    for fmt in sorted({it.fmt for it in items}, key=lambda f: FMT_ORDER.get(f, 9)):
        group = [it for it in items if it.fmt == fmt]
        hashed = [it for it in group if it.phash is not None]
        plain = [it for it in group if it.phash is None]
        if hashed:
            remaining = hashed[:]
            ordered = [remaining.pop(0)]
            while remaining:
                last = ordered[-1].phash
                j = min(range(len(remaining)),
                        key=lambda i: hamming(remaining[i].phash, last))
                ordered.append(remaining.pop(j))
            out.extend(ordered)
        out.extend(plain)
    return out


def build_view(cat: Catalog, bot_username: str = "") -> tuple[list[dict], dict]:
    # First time only: seed the manual order with the look-alike-grouped
    # similarity order (a nice starting point). After that, always use the saved
    # position order so the user's drag-drop arrangement is what shows/publishes.
    if cat.get_meta("order_seeded") != "1":
        seeded = order_by_similarity(cat.all_items())
        cat.set_order([it.content_key for it in seeded])
        cat.set_meta("order_seeded", "1")
    items = cat.all_items()  # saved manual/seeded order (by position)

    view = []
    by_key: dict[str, Path] = {}

    logo_path = Path(BRAND_LOGO_DEFAULT)
    if bot_username.lower() in BRAND_LOGO_BOTS and logo_path.is_file():
        # Preview-only: shows where the brand logo will be inserted on publish.
        # It is NOT part of the catalog, is never counted in the totals, is not
        # clickable/toggleable, and is never sent to /api/save.
        view.append({
            "key": LOGO_KEY, "fmt": "static", "label": "Brand logo (auto-added on publish)",
            "emoji": "", "included": True, "isLogo": True,
        })
        by_key[LOGO_KEY] = logo_path

    for it in items:
        label = (it.keywords[0] if it.keywords else
                 (it.emojis[0] if it.emojis else it.content_key[2:10]))
        view.append({
            "key": it.content_key,
            "fmt": it.fmt,
            "label": label,
            "emoji": it.emojis[0] if it.emojis else "",
            "included": it.included,
        })
        by_key[it.content_key] = Path(it.file_path)
    return view, by_key


def make_handler(view: list[dict], by_key: dict, db_path: Path):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet default logging
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            # Swallow benign disconnects (browser navigated away / cancelled a
            # media request): these raise ConnectionAbortedError/BrokenPipeError
            # on Windows and only spam the log with harmless tracebacks.
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def handle_one_request(self):
            # Same for header/parse-level disconnects.
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE.replace("__ITEMS__", json.dumps(view))
                           .encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path.startswith("/img/"):
                key = unquote(self.path[len("/img/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                data = it.read_bytes()
                self._send(200, data, _MIME.get(it.suffix.lower(), "application/octet-stream"))
                return
            if self.path.startswith("/lottie/"):
                key = unquote(self.path[len("/lottie/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"{}")
                    return
                try:
                    raw = it.read_bytes()
                    if raw[:2] == b"\x1f\x8b":          # gzip-compressed .tgs
                        raw = gzip.decompress(raw)
                    self._send(200, raw, "application/json")
                except Exception:  # noqa: BLE001
                    self._send(500, b"{}")
                return
            if self.path.startswith("/static/"):
                name = unquote(self.path[len("/static/"):])
                f = (ASSET_DIR / name)
                if f.is_file() and f.parent == ASSET_DIR:   # no traversal
                    ctype = "application/javascript" if f.suffix == ".js" else "application/octet-stream"
                    self._send(200, f.read_bytes(), ctype)
                else:
                    self._send(404, b"not found", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return

            if self.path == "/api/save":
                excluded = set(payload.get("excluded", []))
                with lock:
                    cat = Catalog(db_path)
                    try:
                        inc, exc = cat.set_inclusion(excluded)
                    finally:
                        cat.close()
                    for v in view:
                        v["included"] = v["key"] not in excluded
                self._send(200, json.dumps({"ok": True, "included": inc, "excluded": exc}).encode())
                return

            if self.path == "/api/order":
                # Persist the manual drag-drop order. The logo preview key is
                # ignored (it's not a catalog item).
                keys = [k for k in payload.get("order", []) if k != LOGO_KEY]
                with lock:
                    cat = Catalog(db_path)
                    try:
                        cat.set_order(keys)
                        cat.set_meta("order_seeded", "1")
                    finally:
                        cat.close()
                    # Reorder the in-memory view to match (keep the logo first).
                    pos = {k: i for i, k in enumerate(keys)}
                    view.sort(key=lambda v: (not v.get("isLogo"), pos.get(v["key"], 1 << 30)))
                self._send(200, json.dumps({"ok": True, "count": len(keys)}).encode())
                return

            self._send(404, b"{}")

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Emoji Mapper — Curate</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='10' fill='%2322d3ee'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root{
  --bg:#06080d; --panel:#0c111b; --panel2:#11182633; --line:#1e2a3a;
  --txt:#e6eef8; --muted:#8aa0b8; --neon:#22d3ee; --neon2:#38bdf8; --bad:#f43f5e;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 800px at 70% -10%,#0b1a2b 0%,var(--bg) 60%);
  color:var(--txt);font-family:Inter,system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;backdrop-filter:blur(10px);
  background:linear-gradient(180deg,#0a0f1aee,#0a0f1abb);border-bottom:1px solid var(--line);
  padding:14px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
h1{font-size:18px;margin:0;font-weight:700;letter-spacing:.3px;
  text-shadow:0 0 12px #22d3ee66}
h1 .dot{color:var(--neon)}
.count{color:var(--muted);font-size:13px;margin-left:4px}
.count b{color:var(--neon2)}
.spacer{flex:1}
button{font:inherit;cursor:pointer;border-radius:10px;border:1px solid var(--line);
  background:#0e1626;color:var(--txt);padding:9px 14px;transition:all .18s ease}
button:hover{border-color:var(--neon);box-shadow:0 0 0 1px #22d3ee55,0 0 14px #22d3ee33}
button.primary{background:linear-gradient(180deg,#0ea5b7,#0b7c8b);border-color:#22d3ee;
  color:#021016;font-weight:700;text-shadow:none}
button.primary:hover{box-shadow:0 0 18px #22d3ee88}
button:focus-visible{outline:2px solid var(--neon2);outline-offset:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:14px;padding:18px 20px 80px}
.card{position:relative;border:1px solid var(--line);border-radius:14px;background:var(--panel);
  padding:12px 10px 10px;text-align:center;cursor:pointer;user-select:none;
  transition:border-color .18s,box-shadow .18s,opacity .18s,transform .05s}
.card:hover{border-color:var(--neon2);box-shadow:0 0 0 1px #38bdf855,0 0 18px #38bdf833}
.card:active{transform:scale(.985)}
.card.on{border-color:var(--neon);box-shadow:0 0 0 1px #22d3ee66,0 0 16px #22d3ee2e}
.card.off{opacity:.42;filter:grayscale(.9)}
.thumb{width:108px;height:108px;margin:0 auto;border-radius:10px;display:flex;
  align-items:center;justify-content:center;overflow:hidden;
  box-shadow:inset 0 0 0 1px #00000026, inset 0 0 0 2px #ffffff14}
/* Backdrops so black / hollow / faint emoji are all visible. Default = checker.
   Dark-friendly mid-slate checker: light enough to reveal black/hollow emoji,
   dark enough to reveal faint/white emoji, while matching the dark panel. */
body.bg-checker .thumb{background-color:#828c9a;background-image:
  linear-gradient(45deg,#464e5a 25%,transparent 25%),
  linear-gradient(-45deg,#464e5a 25%,transparent 25%),
  linear-gradient(45deg,transparent 75%,#464e5a 75%),
  linear-gradient(-45deg,transparent 75%,#464e5a 75%);
  background-size:16px 16px;
  background-position:0 0,0 8px,8px -8px,-8px 0}
body.bg-light .thumb{background:#f4f6f9}
body.bg-dark  .thumb{background:#0a0e16}
body.bg-gray  .thumb{background:#808a96}
.thumb img,.thumb video{max-width:104px;max-height:104px;display:block}
.thumb.lottie svg{width:104px!important;height:104px!important}
.ph{font-size:46px;line-height:108px}
.badge{position:absolute;top:8px;left:8px;font-size:10px;letter-spacing:.5px;
  text-transform:uppercase;color:#9fd; background:#06121b;border:1px solid #1c3a44;
  border-radius:6px;padding:2px 6px}
.card{cursor:grab}
.card.drag{opacity:.5;cursor:grabbing}
.card.over{border-color:#a78bfa;box-shadow:0 0 0 2px #a78bfa88,0 0 18px #a78bfa55}
.card.logo{cursor:default;border-color:#fbbf24;box-shadow:0 0 0 1px #fbbf2455,0 0 16px #fbbf2433}
.card.logo:hover{border-color:#fbbf24;box-shadow:0 0 0 1px #fbbf2477,0 0 18px #fbbf2455}
.card.logo .badge{color:#fbbf24;border-color:#5a4415;background:#1a1508}
.card.logo .lbl{color:#fbbf24}
.tick{position:absolute;top:8px;right:8px;width:22px;height:22px;border-radius:7px;
  display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;
  border:1px solid var(--line);background:#0b1422;color:#06202a}
.card.on .tick{background:var(--neon);border-color:var(--neon);box-shadow:0 0 10px #22d3ee88}
.card.off .tick{background:#1a2230;color:var(--bad);border-color:#3a2330}
.lbl{margin-top:9px;font-size:12px;color:var(--txt);word-break:break-word;line-height:1.3}
.sub{font-size:10px;color:var(--muted);margin-top:2px}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(40px);
  background:#0c1622;border:1px solid var(--neon);color:var(--txt);padding:10px 16px;
  border-radius:12px;box-shadow:0 0 22px #22d3ee44;opacity:0;transition:all .25s;pointer-events:none}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body class="bg-checker">
<header>
  <h1>Emoji Mapper <span class="dot">●</span> Curate</h1>
  <span class="count"><b id="selCount">0</b> / <span id="totCount">0</span> selected
    <span style="color:#8aa0b8">· click = toggle · drag = reorder · hover = play</span></span>
  <span class="spacer"></span>
  <button id="all">Select all</button>
  <button id="none">Deselect all</button>
  <button id="inv">Invert</button>
  <button id="bg" title="Switch preview backdrop so black / hollow / faint emoji are visible">Backdrop: Checker</button>
  <button id="save" class="primary">Save selection</button>
</header>
<div class="grid" id="grid"></div>
<div id="toast"></div>
<script src="/static/lottie_svg.min.js"></script>
<script>
const ITEMS = __ITEMS__;
let lastIdx = null;
const grid = document.getElementById('grid');

function thumb(it){
  if(it.isLogo) return `<div class="thumb"><img loading="lazy" src="/img/${encodeURIComponent(it.key)}" alt="${it.label}"></div>`;
  if(it.fmt==='static') return `<div class="thumb"><img loading="lazy" src="/img/${encodeURIComponent(it.key)}" alt="${it.label}"></div>`;
  if(it.fmt==='video') return `<div class="thumb"><video src="/img/${encodeURIComponent(it.key)}" muted loop autoplay playsinline preload="metadata"></video></div>`;
  return `<div class="thumb lottie" data-key="${encodeURIComponent(it.key)}"><span class="ph">${it.emoji||'▶'}</span></div>`;
}
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
// Performance: animated .tgs are rendered with the lightweight CANVAS renderer
// and shown as a STATIC first frame at rest (near-zero CPU). Only the card you
// hover actually plays — so hundreds of emoji no longer melt the CPU / hang the
// page the way autoplay+loop SVG did. Off-screen players are destroyed.
const anims = new Map();
function playAnim(div){ const a=anims.get(div); if(a && !RM){ try{a.play();}catch(_){}}}
function stopAnim(div){ const a=anims.get(div); if(a){ try{a.goToAndStop(0,true);}catch(_){}}}
const io = new IntersectionObserver(entries=>{
  for(const e of entries){
    const div = e.target, key = div.dataset.key;
    if(e.isIntersecting){
      if(!anims.has(div) && window.lottie){
        const ph = div.querySelector('.ph'); if(ph) ph.remove();
        // SVG renderer (the only one in the vendored build). The key to not
        // melting the CPU is NOT autoplaying: we render the first frame and
        // stop, so no requestAnimationFrame runs until you hover this card.
        const a = lottie.loadAnimation({container:div, renderer:'svg', loop:true,
          autoplay:false, path:'/lottie/'+key});
        a.addEventListener('DOMLoaded',()=>{ try{a.goToAndStop(0,true);}catch(_){}});
        anims.set(div,a);
      }
    } else {
      const a = anims.get(div);
      if(a){ try{a.destroy();}catch(_){} anims.delete(div); div.innerHTML='<span class="ph">▶</span>'; }
    }
  }
},{root:null, rootMargin:'120px'});
function cleanupLottie(){ anims.forEach(a=>{try{a.destroy();}catch(_){}}); anims.clear(); io.disconnect(); }
function observeLottie(){ document.querySelectorAll('.thumb.lottie').forEach(d=>io.observe(d)); }
function render(){
  cleanupLottie();
  grid.innerHTML = ITEMS.map((it,i)=>{
    if(it.isLogo){
      return `<div class="card logo" data-i="${i}">
        <span class="badge">logo</span>
        ${thumb(it)}
        <div class="lbl">${(it.label||'').toString().replace(/</g,'&lt;')}</div>
        <div class="sub">always first, not part of the catalog</div>
      </div>`;
    }
    return `<div class="card ${it.included?'on':'off'}" data-i="${i}" draggable="true">
      <span class="badge">${it.fmt}</span>
      <span class="tick">${it.included?'✓':'✕'}</span>
      ${thumb(it)}
      <div class="lbl">${(it.label||'').toString().replace(/</g,'&lt;')}</div>
      <div class="sub">${it.key.slice(0,10)}…</div>
    </div>`;
  }).join('');
  updateCount();
  observeLottie();
  // Play the hovered emoji only; keep the rest as static first frames.
  grid.querySelectorAll('.thumb.lottie').forEach(div=>{
    const card = div.closest('.card');
    card.addEventListener('mouseenter',()=>playAnim(div));
    card.addEventListener('mouseleave',()=>stopAnim(div));
  });
}
function updateCount(){
  const real = ITEMS.filter(x=>!x.isLogo);
  document.getElementById('selCount').textContent = real.filter(x=>x.included).length;
  document.getElementById('totCount').textContent = real.length;
}
function setCard(i){
  const el = grid.querySelector(`.card[data-i="${i}"]`);
  const it = ITEMS[i];
  el.classList.toggle('on',it.included); el.classList.toggle('off',!it.included);
  el.querySelector('.tick').textContent = it.included?'✓':'✕';
}
grid.addEventListener('click',e=>{
  const card = e.target.closest('.card'); if(!card) return;
  const i = +card.dataset.i;
  if(ITEMS[i].isLogo) return;   // preview-only card: not toggleable
  if(e.shiftKey && lastIdx!==null){
    const [a,b]=[Math.min(lastIdx,i),Math.max(lastIdx,i)];
    const val = !ITEMS[i].included;
    for(let k=a;k<=b;k++){ if(ITEMS[k].isLogo) continue; ITEMS[k].included=val;setCard(k); }
  } else {
    ITEMS[i].included=!ITEMS[i].included; setCard(i);
  }
  lastIdx=i; updateCount();
});

// --- Drag & drop reordering (sets the publish order) --------------------
let dragFrom = null;
let orderTimer = null;
function saveOrder(){
  clearTimeout(orderTimer);
  orderTimer = setTimeout(async ()=>{
    const order = ITEMS.filter(x=>!x.isLogo).map(x=>x.key);
    try{
      await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({order})});
      toast('Order saved ✓');
    }catch(_){ toast('Could not save order'); }
  }, 400);
}
grid.addEventListener('dragstart',e=>{
  const card=e.target.closest('.card'); if(!card){e.preventDefault();return;}
  const i=+card.dataset.i;
  if(ITEMS[i].isLogo){ e.preventDefault(); return; }   // logo is fixed first
  dragFrom=i; card.classList.add('drag');
  e.dataTransfer.effectAllowed='move';
  try{e.dataTransfer.setData('text/plain',String(i));}catch(_){}
});
grid.addEventListener('dragover',e=>{
  if(dragFrom===null) return;
  e.preventDefault(); e.dataTransfer.dropEffect='move';
  const card=e.target.closest('.card');
  grid.querySelectorAll('.card.over').forEach(c=>c.classList.remove('over'));
  if(card && !ITEMS[+card.dataset.i].isLogo) card.classList.add('over');
});
grid.addEventListener('drop',e=>{
  if(dragFrom===null) return;
  e.preventDefault();
  const card=e.target.closest('.card');
  let to = card ? +card.dataset.i : ITEMS.length-1;
  const firstMovable = ITEMS.findIndex(x=>!x.isLogo);
  if(to < firstMovable) to = firstMovable;            // never before the logo
  if(to!==dragFrom){
    const [moved]=ITEMS.splice(dragFrom,1);
    ITEMS.splice(to,0,moved);
    render();
    saveOrder();
  }
  dragFrom=null;
});
grid.addEventListener('dragend',()=>{
  dragFrom=null;
  grid.querySelectorAll('.card.over,.card.drag').forEach(c=>c.classList.remove('over','drag'));
});

document.getElementById('all').onclick=()=>{ITEMS.forEach(x=>{if(!x.isLogo)x.included=true;});render();};
document.getElementById('none').onclick=()=>{ITEMS.forEach(x=>{if(!x.isLogo)x.included=false;});render();};
document.getElementById('inv').onclick=()=>{ITEMS.forEach(x=>{if(!x.isLogo)x.included=!x.included;});render();};
// Preview backdrop switcher: makes black / hollow / faint emoji visible.
const BGS=['checker','light','dark','gray'];
const BGLABEL={checker:'Checker',light:'Light',dark:'Dark',gray:'Gray'};
function applyBg(b){
  BGS.forEach(x=>document.body.classList.remove('bg-'+x));
  document.body.classList.add('bg-'+b);
  document.getElementById('bg').textContent='Backdrop: '+BGLABEL[b];
  try{localStorage.setItem('emojiBg',b);}catch(_){}
}
document.getElementById('bg').onclick=()=>{
  const cur=BGS.find(x=>document.body.classList.contains('bg-'+x))||'checker';
  applyBg(BGS[(BGS.indexOf(cur)+1)%BGS.length]);
};
applyBg((()=>{try{return localStorage.getItem('emojiBg')||'checker';}catch(_){return 'checker';}})());
document.getElementById('save').onclick=async()=>{
  const excluded = ITEMS.filter(x=>!x.isLogo && !x.included).map(x=>x.key);
  const r = await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({excluded})});
  const j = await r.json();
  toast(`Saved ✓  ${j.included} included · ${j.excluded} excluded`);
};
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;
  t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}
render();
</script>
</body></html>"""


def _detect_bot_username() -> str:
    """Best-effort: which bot's token is configured, so the panel can preview
    the brand logo only when it would actually be added on publish (i.e. the
    Emoji Mapper bot, never the coin bot). Never raises -- on any error
    (missing .env, no network, bad token) the logo preview is simply skipped.
    """
    try:
        from build_pack import Telegram, load_env
        load_env()
        token = os.environ.get("GENERAL_BOT_TOKEN", "")
        if not token:
            return ""
        return Telegram(token).get_me().get("username", "")
    except Exception as exc:  # noqa: BLE001 - preview-only, never fatal
        log.debug("bot username detection failed: %s", exc)
        return ""


def main() -> int:
    setup_logging("panel")
    ap = argparse.ArgumentParser(description="Curate downloaded emoji before publishing.")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser.")
    args = ap.parse_args()

    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    db_path = data_dir / "catalog.db"
    if not db_path.is_file():
        log.error("no catalog at %s (run fetch_pack.py / add_media.py first).", db_path)
        return 2

    bot_username = _detect_bot_username()

    cat = Catalog(db_path)
    try:
        view, by_key = build_view(cat, bot_username)
    finally:
        cat.close()
    log.info("loaded %d emoji from %s", len(view), db_path)

    handler = make_handler(view, by_key, db_path)

    class QuietServer(ThreadingHTTPServer):
        # Don't dump a traceback when a browser simply drops a connection
        # (very common while scrolling a media-heavy grid on Windows).
        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    httpd = QuietServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    log.info("Panel at %s  (Ctrl+C to stop)", url)
    print(f"Emoji curate panel: {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
