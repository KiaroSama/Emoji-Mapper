"""Turn one pack roster into a self-contained HTML page you can actually look at.

The JSON beside it answers "what is in this pack"; this answers "what does it
LOOK like", for a human and equally for a machine that reads pictures. So the
page is one file with nothing to fetch: every thumbnail is a ``data:`` URI, and
the whole roster is repeated verbatim in a ``<script type="application/json">``
block so a parser never has to scrape the markup it was given for looking at.

Animation survives without a single line of player code. An **animated WebP**
plays natively in every current browser, so a Lottie is rasterised once into one
(the same trick the curate panel uses) and a ``.webm`` emoji is embedded as
itself. No JS, no library, no CDN -- which is also why the page still works from
a file:// path years from now.

Thumbnails are cached in ``packs/.thumbs/<custom_emoji_id>.<ext>``. Rasterising
a 181-frame Lottie is ~300-500 ms and an estate refresh touches ~6600 emoji, so
without the cache a routine rebuild would cost most of an hour; with it, only
what actually changed is re-rendered. The key is the id rather than the file
path because a replaced sticker gets a NEW id, which is exactly when the picture
must be re-made.
"""
from __future__ import annotations

import base64
import html
import logging
from pathlib import Path

from PIL import Image

from emojikit import media

log = logging.getLogger("pack_gallery")

THUMB = 88          # px. Big enough to recognise, small enough that 200 of them
                    # inline stay a page rather than a download.
THUMB_FPS = 9       # a roster is for telling emoji apart, not for admiring the
                    # motion; fps is the biggest lever on the inlined weight.
THUMB_QUALITY = 65
_MIME = {".webp": "image/webp", ".webm": "video/webm", ".png": "image/png"}


def _thumb_file(src: Path, fmt: str, dest_stem: Path) -> Path | None:
    """The moving thumbnail (or the only one, for a static emoji)."""
    try:
        if fmt == "animated":
            out = dest_stem.with_suffix(".webp")
            media.lottie_preview_webp(src, out, size=THUMB, fps=THUMB_FPS,
                                      quality=THUMB_QUALITY)
            return out
        if fmt == "video":
            # Embedded as itself: 100x100 VP9 is already tiny, and re-encoding
            # would only cost quality for no size win.
            out = dest_stem.with_suffix(".webm")
            out.write_bytes(src.read_bytes())
            return out
        out = dest_stem.with_suffix(".webp")
        with Image.open(src) as im:
            im = im.convert("RGBA")
            im.thumbnail((THUMB, THUMB), Image.LANCZOS)
            # exact=True or libwebp rewrites the RGB under transparent pixels --
            # the same trap owner rule 1 meets on republished art.
            im.save(out, format="WEBP", quality=THUMB_QUALITY, exact=True)
        return out
    except Exception as exc:                      # noqa: BLE001 - one bad file must not lose the page
        log.warning("thumb failed for %s (%s): %s", src.name, fmt, exc)
        return None


def _still_file(src: Path, fmt: str, dest_stem: Path) -> Path | None:
    """Frame 0 of an animated emoji, so the page can show it WITHOUT decoding.

    This is what makes a 200-card roster affordable: every card starts frozen
    and only the ones actually on screen are swapped to the moving version, the
    same gating the curate panel uses. Without it the browser decodes 146
    animations at once and the page is the "heavy" the owner reported.
    """
    if fmt != "animated":
        return None
    out = dest_stem.with_name(dest_stem.name + "_still").with_suffix(".webp")
    if out.is_file():
        return out
    try:
        media.lottie_still_webp(src, out, size=THUMB, quality=THUMB_QUALITY)
        return out
    except Exception as exc:                      # noqa: BLE001
        log.warning("still failed for %s: %s", src.name, exc)
        return None


def _uri(path: Path) -> str:
    mime = _MIME.get(path.suffix, "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def thumb_uri(cid: str, src: Path | None, fmt: str, cache: Path):
    """``(kind, moving-uri, still-uri|None)`` for one emoji, rendered once."""
    if not src or not src.is_file():
        return None
    cache.mkdir(parents=True, exist_ok=True)
    stem = cache / cid
    existing = [p for p in (stem.with_suffix(".webp"), stem.with_suffix(".webm")) if p.is_file()]
    out = existing[0] if existing else _thumb_file(src, fmt, stem)
    if not out or not out.is_file():
        return None
    still = _still_file(src, fmt, stem)
    kind = "video" if out.suffix == ".webm" else ("anim" if fmt == "animated" else "img")
    return kind, _uri(out), (_uri(still) if still else None)


_CSS = """
/* Deliberately the curate panel's visual language -- same grid, same card, same
   per-format accent, same checker backdrop -- so the two read as one product.
   What is NOT here is everything that MUTATES: no draggable, no tick, no save,
   no selection state. This page is for looking, and a control that looks live
   but changes nothing is worse than no control. */
:root{color-scheme:dark;--bg:#0b0f14;--panel:#111820;--line:#1f2a36;--txt:#e6edf6;
  --muted:#8fa0b8;--neon:#22d3ee;--neon2:#7dd3fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.45 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0e141c;border-bottom:1px solid var(--line);
  padding:14px 20px}
h1{margin:0 0 4px;font-size:18px}
.meta{color:var(--muted);font-size:12px;line-height:1.5}
.meta a{color:var(--neon2)}
.meta b{color:var(--txt)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:14px;padding:18px 20px 60px}
.card{position:relative;border:2px solid var(--line);border-radius:14px;
  background:var(--panel);padding:12px 10px 10px;text-align:center;
  /* Skip layout/paint off-screen: a coin pack is 200 cards and pack 1 is 200
     more, most of them animated. The size hint stops the scrollbar jumping. */
  content-visibility:auto;contain-intrinsic-size:auto 268px}
.card.fmt-static  {--fmt:#22d3ee;--fmtInk:#9fe8f5;--fmtBg:#06121b;--fmtLine:#1c3a44}
.card.fmt-animated{--fmt:#c4b5fd;--fmtInk:#ede9fe;--fmtBg:#140f28;--fmtLine:#4c3f7a}
.card.fmt-video   {--fmt:#34d399;--fmtInk:#a7f3d0;--fmtBg:#04170f;--fmtLine:#1b4a38}
.card.logo{border-color:#6b5316;--fmt:#fbbf24;--fmtInk:#fde68a;--fmtBg:#1a1508;--fmtLine:#5a4415}
.hdr{display:flex;flex-direction:column;align-items:center;gap:3px;margin:0 0 8px}
.pos{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--neon2);
  background:#08131f;border:1px solid #1c3a44;border-radius:6px;padding:2px 6px;min-width:24px}
.badge{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:var(--fmtInk,#9fd);
  background:var(--fmtBg,#06121b);border:1px solid var(--fmtLine,#1c3a44);
  border-radius:6px;padding:2px 5px}
.thumb{width:108px;height:108px;margin:3px auto 4px;border-radius:10px;display:flex;
  align-items:center;justify-content:center;overflow:hidden;
  outline:2px solid var(--fmt,#2b6f7d);outline-offset:2px;
  box-shadow:inset 0 0 0 1px #00000026}
.thumb img,.thumb video{max-width:104px;max-height:104px;display:block}
.thumb.empty{background:#0a0e16;color:#61708a;font-size:11px}
/* The glyph the sticker carries. Invisible in Telegram -- only the picture is
   ever shown -- so the only place it can be checked against the art is here. */
.glyph{font-size:20px;line-height:1.1;margin:6px 0 2px}
.nm{font-size:11px;color:var(--muted);word-break:break-word;margin-bottom:6px}
/* Both ids copy on click. The source id is the one an external map may still
   point at, so it is as load-bearing as ours and gets the same affordance. */
.id{display:block;font:11px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;
  border-radius:6px;padding:3px 4px;margin-top:3px;cursor:copy;word-break:break-all;
  border:1px solid transparent}
.id:hover{border-color:var(--neon2);background:#0b1a26}
.id.now{color:#c9d6e8}
.id.was{color:#e0a86a}
.id .k{display:block;font-size:9px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted)}
.card.copied .id.hit{border-color:#22c55e;color:#86efac}

/* The view-only half of the panel's header. Top/Bottom, the backdrop cycle and
   the animation switch change nothing about the DATA -- they are how you SEE a
   grid of a thousand emoji -- so leaving them out was over-stripping. */
header .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px}
button{font:inherit;line-height:1.25;cursor:pointer;border-radius:10px;
  border:1px solid var(--line);background:#131b25;color:var(--txt);padding:6px 11px}
button:hover{border-color:var(--neon2)}
.switch{display:inline-flex;align-items:center;gap:8px}
.knob{width:26px;height:15px;border-radius:9px;background:#2b3a4c;position:relative;
  transition:background .15s}
.knob::after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;
  border-radius:50%;background:#8fa0b8;transition:transform .15s,background .15s}
.switch[aria-pressed="true"] .knob{background:#12603f}
.switch[aria-pressed="true"] .knob::after{transform:translateX(11px);background:#34d399}
.sep{width:1px;height:20px;background:var(--line)}
/* Backdrops so black / hollow / faint emoji are all visible -- the same four
   the panel offers, because the same emoji are being judged. */
body.bg-checker .thumb{background-color:#828c9a;background-image:
  linear-gradient(45deg,#464e5a 25%,transparent 25%),
  linear-gradient(-45deg,#464e5a 25%,transparent 25%),
  linear-gradient(45deg,transparent 75%,#464e5a 75%),
  linear-gradient(-45deg,transparent 75%,#464e5a 75%);
  background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0}
body.bg-light .thumb{background:#f4f6f9;background-image:none}
body.bg-dark  .thumb{background:#0a0e16;background-image:none}
body.bg-gray  .thumb{background:#808a96;background-image:none}
"""

_JS = """
// Click either id to copy it. Nothing else on this page reacts to a click:
// it is a record, not an editor.
document.addEventListener('click', e => {
  const el = e.target.closest('.id');
  if (!el || !navigator.clipboard) return;
  const text = el.dataset.id || '';
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const card = el.closest('.card');
    el.classList.add('hit'); card.classList.add('copied');
    setTimeout(() => { el.classList.remove('hit'); card.classList.remove('copied'); }, 700);
  }, () => {});
});

// --- media gating, ported from the curate panel -------------------------- //
// A roster can hold 200 cards, most of them animated. Decoding all of them at
// once is what made this page heavy, so every animated card starts on its STILL
// and only what is actually on screen is swapped to the moving version. The two
// URIs are both inline, so a swap costs no request.
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
let ANIM_ON = false;
try { ANIM_ON = localStorage.getItem('rosterAnim') !== '0'; } catch (_) { ANIM_ON = true; }

// Observes the CARD rather than the media inside it: the card is the element
// carrying content-visibility, so it is the one the browser is already deciding
// about. (An earlier note here blamed content-visibility for the observer never
// firing during testing -- that was wrong. The test browser reported
// visibilityState "hidden" with innerHeight 0, where nothing intersects
// anything and no animation SHOULD start. Same gating as the curate panel.)
const cards = () => document.querySelectorAll('.card');
const mediaIn = (card) => card.querySelector('img[data-anim], video[data-play]');
function play(v, on) {
  try { if (on) { const q = v.play(); if (q) q.catch(() => {}); } else v.pause(); } catch (_) {}
}
function setLive(n, live) {
  if (!n) return;
  if (n.dataset.play) { play(n, live); return; }
  const want = live ? n.dataset.anim : n.dataset.still;
  if (want && n.getAttribute('src') !== want) n.src = want;
}
function freezeAll() { cards().forEach(c => setLive(mediaIn(c), false)); }
const io = window.IntersectionObserver ? new IntersectionObserver(es => {
  for (const e of es) setLive(mediaIn(e.target), e.isIntersecting && ANIM_ON && !RM);
}, { root: null, rootMargin: '0px' }) : null;

function applyAnim() {
  const b = document.getElementById('anim');
  b.setAttribute('aria-pressed', ANIM_ON ? 'true' : 'false');
  document.getElementById('animLabel').textContent = 'Animation: ' + (ANIM_ON ? 'On' : 'Off');
  if (!ANIM_ON || RM) { freezeAll(); return; }
  cards().forEach(c => { if (io) { io.unobserve(c); io.observe(c); } });
}
// Scrolling is the one moment the decoding buys nothing: the frames go past too
// fast to read while the compositor is already busy.
let thaw = null;
addEventListener('scroll', () => {
  if (!ANIM_ON || RM) return;
  if (thaw === null) freezeAll(); else clearTimeout(thaw);
  thaw = setTimeout(() => { thaw = null; applyAnim(); }, 180);
}, { passive: true });
document.addEventListener('visibilitychange',
  () => { if (document.hidden) freezeAll(); else applyAnim(); });

document.getElementById('anim').addEventListener('click', () => {
  ANIM_ON = !ANIM_ON;
  try { localStorage.setItem('rosterAnim', ANIM_ON ? '1' : '0'); } catch (_) {}
  applyAnim();
});

// --- the other view-only controls ---------------------------------------- //
const BGS = ['checker', 'light', 'dark', 'gray'];
const BGLABEL = { checker: 'Checker', light: 'Light', dark: 'Dark', gray: 'Gray' };
function applyBg(b) {
  BGS.forEach(x => document.body.classList.remove('bg-' + x));
  document.body.classList.add('bg-' + b);
  document.getElementById('bg').textContent = 'Backdrop: ' + BGLABEL[b];
  try { localStorage.setItem('rosterBg', b); } catch (_) {}
}
document.getElementById('bg').addEventListener('click', () => {
  const cur = BGS.find(x => document.body.classList.contains('bg-' + x)) || 'checker';
  applyBg(BGS[(BGS.indexOf(cur) + 1) % BGS.length]);
});
// scrollTo, not scrollIntoView: the latter aligns with the viewport top, which
// sits behind the sticky header, so Top stopped one header short.
document.getElementById('top').addEventListener('click',
  () => scrollTo({ top: 0, behavior: 'smooth' }));
document.getElementById('bot').addEventListener('click',
  () => scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));

let saved = 'checker';
try { saved = localStorage.getItem('rosterBg') || 'checker'; } catch (_) {}
applyBg(BGS.includes(saved) ? saved : 'checker');
applyAnim();
"""


def render(doc: dict, media_of, cache: Path) -> str:
    """The page for one roster document.

    ``media_of(entry) -> Path | None`` is supplied by the caller because the two
    families keep their art in completely different places, and this module has
    no business knowing which is which.
    """
    cards, drawn, moving = [], 0, 0
    for e in doc["emoji"]:
        cid = e["custom_emoji_id"]
        got = thumb_uri(cid, media_of(e), e["format"], cache)
        if got:
            kind, uri, still = got
            drawn += 1
            if kind == "video":
                # preload=none and paused: 15 of these decoding at once was a
                # measurable part of "heavy". The observer starts the visible ones.
                art = (f'<video src="{uri}" data-play="1" loop muted playsinline '
                       f'preload="none"></video>')
                moving += 1
            elif kind == "anim" and still:
                # Starts FROZEN. Both sources are inline, so the swap the
                # observer makes costs no request.
                art = (f'<img src="{still}" data-still="{still}" data-anim="{uri}" '
                       f'alt="emoji {e["index"]}">')
                moving += 1
            else:
                art = f'<img src="{uri}" alt="emoji {e["index"]}" loading="lazy">'
            thumb_cls = "thumb"
        else:
            art, thumb_cls = "no preview", "thumb empty"
        was = ", ".join(e["source_emoji_ids"])
        logo = e["role"] == "brand-logo"
        ids = (f'<span class="id now" data-id="{html.escape(cid)}" '
               f'title="click to copy"><span class="k">this pack</span>'
               f'{html.escape(cid)}</span>')
        if was:
            # The id it had where we took it from. External maps may still point
            # at it, so it is worth copying too -- not just worth reading.
            ids += (f'<span class="id was" data-id="{html.escape(was)}" '
                    f'title="click to copy"><span class="k">original pack</span>'
                    f'{html.escape(was)}</span>')
        cards.append(
            f'<div class="card {"logo" if logo else "fmt-" + html.escape(e["format"])}"'
            f' data-index="{e["index"]}" data-slot="{e["slot"]}"'
            f' data-custom-emoji-id="{html.escape(cid)}"'
            f' data-format="{html.escape(e["format"])}"'
            f' data-glyph="{html.escape(e["glyph"] or "")}"'
            f' data-source-emoji-ids="{html.escape(was)}">'
            f'<div class="hdr"><span class="pos">{e["index"]}</span>'
            f'<span class="badge">{"logo" if logo else html.escape(e["format"])}</span></div>'
            f'<div class="{thumb_cls}">{art}</div>'
            f'<div class="glyph" title="the glyph this sticker carries">'
            f'{html.escape(e["glyph"] or "—")}</div>'
            f'<div class="nm">{html.escape(e["name"] or "")}</div>'
            f'{ids}</div>')

    import json as _json
    title = html.escape(doc.get("title") or doc["set_name"])
    zero_note = (
        "<b>#</b> counts from 0 and emoji 0 is the brand logo; <b>slot</b> is the "
        "position Telegram shows. <b>was</b> is the id this emoji had in the pack "
        "it came from."
        if doc["family"] == "general" else
        "<b>#</b> counts from 0; <b>slot</b> is the position Telegram shows. This "
        "family carries no brand logo, so emoji 0 is a real coin.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - emoji roster</title>
<style>{_CSS}</style></head><body class="bg-checker">
<header>
<h1>{title}</h1>
<div class="meta">
<code>{html.escape(doc["set_name"])}</code> &middot;
{doc["count"]} emoji ({drawn} with a preview) &middot;
{html.escape(doc["family"])} pack {doc.get("pack_index")} &middot;
<a href="{html.escape(doc["link"])}">{html.escape(doc["link"])}</a><br>
Captured {html.escape(doc["captured_utc"])} live from Telegram.
{zero_note}
</div>
<div class="bar">
<button id="top" title="Jump to the first card">&#8593; Top</button>
<button id="bot" title="Jump to the last card">&#8595; Bottom</button>
<span class="sep"></span>
<button id="bg" title="Switch the preview backdrop so black / hollow / faint emoji are visible">Backdrop: Checker</button>
<button id="anim" class="switch" aria-pressed="true" title="Freeze every animation on its first frame. The lightest this page gets -- nothing is decoding."><span class="knob"></span><span id="animLabel">Animation: On</span></button>
<span class="sep"></span>
<span class="meta">{moving} of {doc["count"]} animate &middot; click either id to copy it</span>
</div></header>
<main class="grid">
{chr(10).join(cards)}
</main>
<script type="application/json" id="roster">
{_json.dumps(doc, ensure_ascii=False, indent=1)}
</script>
<script>{_JS}</script>
</body></html>
"""
