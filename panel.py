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
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from build_collection import BRAND_LOGO_BOTS, BRAND_LOGO_DEFAULT
from emojikit.catalog import PHASH_BITS, Catalog
from emojikit.logsetup import record_exit_code, setup_logging
from emojikit.media import (PREVIEW_FPS, lottie_preview_webp,
                            lottie_still_webp)

ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets"
log = logging.getLogger("panel")

_MIME = {".webp": "image/webp", ".png": "image/png", ".gif": "image/gif",
         ".webm": "video/webm", ".tgs": "application/gzip"}
FMT_ORDER = {"static": 0, "video": 1, "animated": 2}
LOGO_KEY = "__brand_logo__"  # pseudo content_key: preview-only, never saved/counted

MAX_BODY = 4 * 1024 * 1024  # generous for an order list, small enough to bound

# Media is content-addressed (the key IS the content hash), so a served file can
# never change under a key -- immutable caching is safe and stops the browser
# re-fetching every thumbnail while you scroll or re-sort.
_IMMUTABLE = "public, max-age=31536000, immutable"


def _json_for_script(value) -> str:
    """JSON safe to embed in an inert <script type=application/json> block.

    ``</script>`` inside a catalog label would otherwise close the block and
    everything after it becomes markup. U+2028/U+2029 are escaped because they
    are literal line terminators in JS string context.
    """
    return (json.dumps(value)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


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
            # The walk stays quadratic on purpose: the greedy nearest-neighbour
            # chain IS the look-alike grouping the panel is for, and every index
            # that would make it sub-quadratic (LSH buckets, BK-tree pruning)
            # changes which near-twin ends up next to which. Only the constant
            # is negotiable, so the distance is inlined rather than called:
            # ``(a ^ b).bit_count()`` is media.hamming's exact result, and at
            # n=3 600 dropping the per-pair call costs 0.48 s instead of 0.92 s
            # for a byte-identical order. (Against the older string-building
            # hamming it was 3.7 s.)
            # ponytail: O(n^2) scan; revisit only if a catalog grows past ~10k
            # items AND a different grouping is acceptable.
            remaining = hashed[:]
            ordered = [remaining.pop(0)]
            hashes = [it.phash for it in remaining]
            last = ordered[0].phash
            while remaining:
                best, best_d = 0, PHASH_BITS + 1
                for i, h in enumerate(hashes):
                    d = (h ^ last).bit_count()
                    if d < best_d:
                        best, best_d = i, d
                        if d == 0:
                            break   # nothing beats 0, and min() takes the first
                ordered.append(remaining.pop(best))
                last = hashes.pop(best)
            out.extend(ordered)
        out.extend(plain)
    return out


# The collector labels an ingested emoji "premium-id:<id>", and that id is the
# one thing anyone wants off this page. Decided here rather than by a regex
# inside the page's JavaScript, so it can actually be tested.
_PREMIUM_ID = re.compile(r"^premium-id:(\d+)$")


def copy_id_for(label: str) -> str:
    """The id a label offers for copying, or "" when it offers none.

    Anchored on purpose: "xpremium-id:12" and "premium-id:12x" are not ids, and
    a label that merely CONTAINS digits is not one either.
    """
    m = _PREMIUM_ID.match(label or "")
    return m.group(1) if m else ""


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
            "copyId": copy_id_for(label),
            "emoji": it.emojis[0] if it.emojis else "",
            "included": it.included,
        })
        by_key[it.content_key] = Path(it.file_path)
    return view, by_key


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(netloc: str) -> bool:
    """True if a Host/Origin authority points at this machine's loopback."""
    host = netloc.rsplit("://", 1)[-1]
    if host.startswith("["):                    # [::1]:8765
        host = host[:host.index("]") + 1] if "]" in host else host
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


# Animated previews are rendered once and kept on disk. The work is ~300-500 ms
# per animation, and the server is threaded, so a scrolling browser will ask for
# the same key from several connections at once -- one lock per key collapses
# that to a single render instead of N identical ones fighting for the CPU.
_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


def _preview_bytes(key: str, src: Path, db_path: Path, fps: int,
                   still: bool = False) -> bytes:
    """Preview for a .tgs, rendered once and cached on disk.

    ``still`` gives frame 0 as a single-frame WebP, which is what off-screen
    cards show -- see lottie_still_webp for why that matters.
    """
    cache_dir = db_path.parent / "preview"
    # content_key is a hash of the media, so the name can never go stale; ':'
    # is not legal in a Windows filename. The rate is part of the name because
    # it changes the bytes -- otherwise --preview-fps would silently serve
    # whatever the last run happened to render.
    tag = "still" if still else str(fps)
    dest = cache_dir / f"{key.replace(':', '_')}@{tag}.webp"
    if dest.is_file():
        return dest.read_bytes()
    with _preview_locks_guard:
        per_key = _preview_locks.setdefault(dest.name, threading.Lock())
    with per_key:
        if not dest.is_file():          # another thread may have won the race
            if still:
                lottie_still_webp(src, dest)
            else:
                lottie_preview_webp(src, dest, fps=fps)
        return dest.read_bytes()


def make_handler(view: list[dict], by_key: dict, db_path: Path, token: str,
                 preview_fps: int = PREVIEW_FPS):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet default logging
            pass

        def _send(self, code, body: bytes, ctype="application/json", *,
                  cache: str = ""):
            # Swallow benign disconnects (browser navigated away / cancelled a
            # media request): these raise ConnectionAbortedError/BrokenPipeError
            # on Windows and only spam the log with harmless tracebacks.
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cache:
                    self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def _mutation_allowed(self) -> str:
            """Guard state-changing requests. Returns "" when allowed.

            The panel listens on localhost, so any page in the user's browser
            can POST to it. A per-run token in a custom header cannot be sent
            by a cross-origin ``no-cors`` request and cannot be read by one, so
            it is what actually stops a hostile page from re-ordering or
            de-selecting the catalog.
            """
            if not secrets.compare_digest(
                    self.headers.get("X-Panel-Token", ""), token):
                return "bad or missing panel token"
            if not _is_loopback(self.headers.get("Host", "")):
                return "unexpected Host"
            origin = self.headers.get("Origin")
            if origin is not None and not _is_loopback(origin):
                return "unexpected Origin"
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
            if ctype != "application/json":
                return "Content-Type must be application/json"
            return ""

        def handle_one_request(self):
            # Same for header/parse-level disconnects.
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):
            # Reads need the same Host check as writes. Binding to 127.0.0.1
            # keeps remote sockets out, but any page whose hostname resolves to
            # loopback reaches this server same-origin and can read every
            # response -- and "/" carries the per-run mutation token in its
            # body, which is the one secret the POST guard rests on.
            if not _is_loopback(self.headers.get("Host", "")):
                self._send(403, b"unexpected Host", "text/plain")
                return
            if self.path == "/" or self.path.startswith("/index"):
                # /api/order sorts ``view`` in place, and CPython empties a list
                # for the duration of list.sort(); serialising it unlocked
                # rendered an empty grid.
                with lock:
                    items = _json_for_script(view)
                page = (PAGE.replace("__ITEMS__", items).replace("__TOKEN__", token)
                            .replace("__PREVIEW_FPS__", str(preview_fps)))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                           cache="no-store")
                return
            if self.path.startswith("/img/"):
                key = unquote(self.path[len("/img/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                data = it.read_bytes()
                self._send(200, data,
                           _MIME.get(it.suffix.lower(), "application/octet-stream"),
                           cache=_IMMUTABLE)
                return
            if self.path.startswith("/preview/"):
                # The frame rate is in the URL, not just the disk filename:
                # these are served immutable, so a browser that cached the old
                # rate would keep using it and --preview-fps would look inert.
                path, _, query = self.path.partition("?")
                still = "still=1" in query
                key = unquote(path[len("/preview/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                try:
                    body = _preview_bytes(key, it, db_path, preview_fps, still)
                except Exception as exc:  # noqa: BLE001 - one bad item must not 500 the grid
                    log.warning("preview failed for %s: %s", key, exc)
                    self._send(404, b"no preview", "text/plain")
                    return
                # Keyed by content_key, so the bytes can never change under it.
                self._send(200, body, "image/webp", cache=_IMMUTABLE)
                return
            if self.path.startswith("/static/"):
                name = unquote(self.path[len("/static/"):])
                f = (ASSET_DIR / name).resolve()
                # Resolve first, then require physical containment: comparing
                # parents would reject a legitimate subdirectory and would not
                # stop a symlink pointing outside the tree.
                if f.is_file() and f.is_relative_to(ASSET_DIR.resolve()):
                    ctype = ("application/javascript" if f.suffix == ".js"
                             else _MIME.get(f.suffix.lower(),
                                            "application/octet-stream"))
                    self._send(200, f.read_bytes(), ctype, cache=_IMMUTABLE)
                else:
                    self._send(404, b"not found", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

        def _body_length(self):
            """Declared body size, or None after sending the right 4xx."""
            try:
                n = int(self.headers.get("Content-Length"))
            except (TypeError, ValueError):
                self._send(411, b'{"error":"Content-Length required"}')
                return None
            if n < 0 or n > MAX_BODY:
                self._send(413, b'{"error":"body too large"}')
                return None
            return n

        def _read_body(self, n: int) -> bytes | None:
            try:
                return self.rfile.read(n)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return None

        def do_POST(self):
            try:
                self._route_post()
            except sqlite3.Error as exc:
                # build_collection.py reads the same database file and
                # sqlite3.connect only waits 5 s, so "database is locked" is
                # routine here, not freak. Uncaught it escaped the handler and
                # closed the socket with no HTTP response at all, so the panel
                # could only say "Save failed" with no reason.
                self._send(503, json.dumps(
                    {"error": f"catalog unavailable: {exc}"}).encode())

        def _route_post(self):
            n = self._body_length()
            if n is None:
                return
            # Read the body BEFORE answering, even when the request is going to
            # be rejected: replying to a request whose body is still in flight
            # resets the connection, so the client sees an abort instead of the
            # 403 explaining what was wrong.
            raw = self._read_body(n)
            if raw is None:
                return

            why = self._mutation_allowed()
            if why:
                self._send(403, json.dumps({"error": why}).encode())
                return

            try:
                payload = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                self._send(400, b'{"error":"malformed JSON"}')
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"expected a JSON object"}')
                return

            if self.path == "/api/save":
                if set(payload) - {"excluded"}:
                    self._send(400, b'{"error":"unknown keys"}')
                    return
                raw = payload.get("excluded", [])
                if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
                    self._send(400, b'{"error":"excluded must be a list of keys"}')
                    return
                # ``known`` MUST be read under the lock: /api/order sorts
                # ``view`` in place and CPython empties a list for the duration
                # of list.sort(), so a save landing in that window saw no known
                # keys, intersected the request down to nothing, and
                # set_inclusion(set()) re-included every row -- discarding the
                # whole de-selection while still answering {"ok": true}.
                with lock:
                    known = {v["key"] for v in view if not v.get("isLogo")}
                    excluded = set(raw) & known
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
                # ignored (it's not a catalog item). Anything other than an
                # exact permutation of the current keys is rejected: a partial
                # or padded list would silently drop items from the publish
                # order.
                if set(payload) - {"order"}:
                    self._send(400, b'{"error":"unknown keys"}')
                    return
                raw = payload.get("order", [])
                if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
                    self._send(400, b'{"error":"order must be a list of keys"}')
                    return
                keys = [k for k in raw if k != LOGO_KEY]
                with lock:
                    # Under the lock for the same reason as /api/save, and
                    # because this is check-then-act: validating against a
                    # ``view`` that a concurrent sort has emptied rejects a
                    # perfectly good order as "not a permutation".
                    expected = sorted(v["key"] for v in view if not v.get("isLogo"))
                    ok = sorted(keys) == expected
                    if ok:
                        cat = Catalog(db_path)
                        try:
                            cat.set_order(keys)
                            cat.set_meta("order_seeded", "1")
                        finally:
                            cat.close()
                        # Reorder the in-memory view to match (logo stays first).
                        pos = {k: i for i, k in enumerate(keys)}
                        view.sort(key=lambda v: (not v.get("isLogo"), pos.get(v["key"], 1 << 30)))
                if not ok:
                    self._send(400, b'{"error":"order must be a permutation of current keys"}')
                    return
                self._send(200, json.dumps({"ok": True, "count": len(keys)}).encode())
                return

            self._send(404, b"{}")

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Emoji Mapper — Curate</title>
<link rel="icon" type="image/png" href="/static/logo-128.png">
<style>
/* No webfont import: this panel runs offline on localhost, and an @import to
   fonts.googleapis.com blocks first paint until it times out. */
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
.brand{border-radius:8px;flex:none;filter:drop-shadow(0 0 8px #22d3ee55)}
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
  transition:border-color .18s,box-shadow .18s,opacity .18s,transform .05s;
  /* Skip layout/paint for off-screen cards. This is what keeps thousands of
     emoji scrolling smoothly; the size hint stops the scrollbar jumping. */
  content-visibility:auto;contain-intrinsic-size:auto 186px}
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
.pos{position:absolute;top:8px;left:50%;transform:translateX(-50%);
  font-size:10px;font-weight:700;font-variant-numeric:tabular-nums;
  color:var(--neon2);background:#08131f;border:1px solid #1c3a44;
  border-radius:6px;padding:2px 7px;min-width:26px;pointer-events:none}
.card.off .pos{color:var(--muted)}
.lbl.copyable{cursor:pointer;text-decoration:underline dotted var(--muted);text-underline-offset:2px}
.lbl.copyable:hover{color:var(--neon)}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(40px);
  background:#0c1622;border:1px solid var(--neon);color:var(--txt);padding:10px 16px;
  border-radius:12px;box-shadow:0 0 22px #22d3ee44;opacity:0;transition:all .25s;pointer-events:none}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body class="bg-checker">
<header>
  <img class="brand" src="/static/logo-128.png" alt="" width="30" height="30">
  <h1>Emoji Mapper <span class="dot">●</span> Curate</h1>
  <span class="count"><b id="selCount">0</b> / <span id="totCount">0</span> selected
    <span style="color:#8aa0b8">· click = toggle · drag = reorder · click the id = copy</span></span>
  <span class="spacer"></span>
  <button id="all">Select all</button>
  <button id="none">Deselect all</button>
  <button id="inv">Invert</button>
  <button id="bg" title="Switch preview backdrop so black / hollow / faint emoji are visible">Backdrop: Checker</button>
  <button id="anim" title="Freeze every animation on their first frame. The lightest the panel gets -- nothing is decoding.">Animation: On</button>
  <button id="save" class="primary">Save selection</button>
</header>
<div class="grid" id="grid"></div>
<div id="toast"></div>
<script id="items-data" type="application/json">__ITEMS__</script>
<script>
// Catalog labels are attacker-influenced (they come from downloaded packs), so
// item data is parsed from an inert JSON block and only ever written to the DOM
// with textContent -- never interpolated into markup.
const ITEMS = JSON.parse(document.getElementById('items-data').textContent);
const TOKEN = "__TOKEN__";
const PREVIEW_FPS = __PREVIEW_FPS__;
const grid = document.getElementById('grid');
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const cards = new Map();          // key -> card element
let lastIdx = null;

function el(tag, cls, text){
  const n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text !== undefined) n.textContent = text;
  return n;
}

function makeThumb(it){
  const box = el('div','thumb');
  const src = '/img/' + encodeURIComponent(it.key);
  if(it.fmt === 'video'){
    const v = el('video');
    v.muted = true; v.loop = true; v.playsInline = true;
    // Plays on its own, like the animated cards. Hover-only was rejected: a
    // grid of stills is useless for curating. Bounded the same way instead --
    // the viewport observer starts and pauses playback, so what costs anything
    // is what you can actually see, not the whole catalog.
    v.preload = 'metadata';
    v.dataset.play = '1';
    v.src = src + '#t=0.001';
    box.appendChild(v);
  } else if(it.fmt === 'animated'){
    // An animated WebP, played by the browser itself. This used to be a
    // lottie.js SVG player per card (~704 DOM nodes each, six figures for a
    // full grid) which is what made this panel crawl. One <img> animates on the
    // compositor and costs one node, so every card can play at once again.
    const img = el('img');
    img.loading = 'lazy'; img.decoding = 'async';
    img.width = 104; img.height = 104;
    img.alt = it.label || '';
    // Starts as the still. The observer swaps in the animation when the card is
    // near the viewport -- an animated image the browser cannot show still costs
    // its decoded frames (~2.5 MB each here, 361 MB if all 146 buffer at once).
    const k = encodeURIComponent(it.key);
    img.dataset.anim = '/preview/' + k + '?fps=' + PREVIEW_FPS;
    img.dataset.still = '/preview/' + k + '?still=1';
    img.src = img.dataset.still;
    box.appendChild(img);
  } else {
    const img = el('img');
    // Native lazy loading: the browser already defers off-screen images, and
    // unlike a JS observer it still works if IntersectionObserver never fires.
    img.loading = 'lazy'; img.decoding = 'async';
    img.width = 104; img.height = 104;
    img.alt = it.label || '';     // property assignment: no attribute injection
    img.src = src;
    box.appendChild(img);
  }
  return box;
}

function makeCard(it){
  const card = el('div', it.isLogo ? 'card logo' : 'card ' + (it.included ? 'on' : 'off'));
  card.dataset.key = it.key;
  if(!it.isLogo) card.draggable = true;
  card.appendChild(el('span','badge', it.isLogo ? 'logo' : it.fmt));
  // Filled by renumber(), never here: a number written at build time is right
  // exactly once, and wrong from the first drag onwards.
  if(!it.isLogo) card.appendChild(el('span','pos',''));
  if(!it.isLogo) card.appendChild(el('span','tick', it.included ? '✓' : '✕'));
  card.appendChild(makeThumb(it));
  const lbl = el('div','lbl', it.label || '');
  // copyId is decided server-side (panel.copy_id_for) so it is unit-tested.
  if(it.copyId){ lbl.classList.add('copyable'); lbl.dataset.copy = it.copyId;
                 lbl.title = 'Click to copy ' + it.copyId; }
  card.appendChild(lbl);
  card.appendChild(el('div','sub', it.isLogo
    ? 'always first, not part of the catalog'
    : it.key.slice(0,10) + '…'));
  cards.set(it.key, card);
  return card;
}

// The publish position, recomputed from ITEMS rather than tracked alongside it
// -- ITEMS *is* the order, so anything else is a second copy that can drift.
// Cheap enough to run on every reorder: 200 text writes, no layout thrash.
function renumber(){
  let n = 0;
  for(const it of ITEMS){
    if(it.isLogo) continue;
    n++;
    const card = cards.get(it.key);
    const pos = card && card.querySelector('.pos');
    if(pos && pos.textContent !== String(n)) pos.textContent = n;
  }
}

// Only the cards you can actually see animate. Everything else holds frame 0,
// so the number of live animations is bounded by the viewport rather than by
// the catalog. Swapping an <img> src is cheap -- both URLs are immutable-cached,
// so this never refetches -- which is what makes this affordable where
// mounting/destroying a player was not.
const animIO = window.IntersectionObserver ? new IntersectionObserver(es => {
  for (const e of es) {
    const t = e.target;
    const live = e.isIntersecting && ANIM_ON && !RM;
    if (t.dataset.play) { setPlaying(t, live); continue; }
    const want = live ? t.dataset.anim : t.dataset.still;
    if (want && t.getAttribute('src') !== want) t.src = want;
  }
}, {root: null, rootMargin: '300px'}) : null;

// play() rejects when the element is detached or the browser refuses; that is
// not an error worth surfacing, but it MUST be caught or it becomes an
// unhandled rejection on every scroll.
function setPlaying(v, on){
  try { if (on) { const q = v.play(); if (q) q.catch(()=>{}); } else { v.pause(); } }
  catch(_){}
}

function animatedNodes(){
  return grid.querySelectorAll('img[data-anim], video[data-play]');
}

function observeAnimated(){
  if (!animIO) return;
  animatedNodes().forEach(n => animIO.observe(n));
  // Without an observer nothing would ever start, so fall back to playing all
  // of them rather than showing a grid of frozen videos.
  if (!window.IntersectionObserver) {
    grid.querySelectorAll('video[data-play]').forEach(v => setPlaying(v, ANIM_ON));
  }
}

// Master switch. Off = every card holds frame 0 and nothing decodes at all,
// which is the lightest the grid can be; the observer stops swapping so it
// cannot undo the freeze behind your back.
let ANIM_ON = localStorage.getItem('animOn') !== '0';
function applyAnim(){
  document.getElementById('anim').textContent = 'Animation: ' + (ANIM_ON ? 'On' : 'Off');
  animatedNodes().forEach(n => {
    if (!ANIM_ON) {
      if (n.dataset.play) setPlaying(n, false);
      else if (n.getAttribute('src') !== n.dataset.still) n.src = n.dataset.still;
    } else if (animIO) { animIO.unobserve(n); animIO.observe(n); }  // re-evaluate
  });
}

// --- lazy media ---
// Static and animated thumbs are both plain <img> (loading=lazy), video uses
// preload=metadata. The browser owns all of it: no player objects, no
// IntersectionObserver, no per-card listeners to leak.
//
// This replaced a lottie.js SVG player per animated card. Measured on a
// 146-animation catalog that was ~704 DOM nodes EACH -- 1 426 document nodes
// with none mounted, 8 476 with ten -- and every scroll tore down and rebuilt a
// row's worth. Pre-rendering each .tgs to an animated WebP server-side moves
// the work off the page entirely, so all of them animate at once.

function render(){
  cards.clear();
  const frag = document.createDocumentFragment();
  for(const it of ITEMS) frag.appendChild(makeCard(it));
  grid.textContent = '';
  grid.appendChild(frag);
  observeAnimated();
  applyAnim();
  renumber();
  updateCount();
}
function updateCount(){
  const real = ITEMS.filter(x=>!x.isLogo);
  document.getElementById('selCount').textContent = real.filter(x=>x.included).length;
  document.getElementById('totCount').textContent = real.length;
}
// In-place update: never rebuild the grid just to flip a selection.
function setCard(it){
  const el2 = cards.get(it.key); if(!el2) return;
  el2.classList.toggle('on',it.included); el2.classList.toggle('off',!it.included);
  const tick = el2.querySelector('.tick');
  if(tick) tick.textContent = it.included ? '✓' : '✕';
}
function setAll(fn){ for(const it of ITEMS){ if(it.isLogo) continue; it.included = fn(it); setCard(it); } updateCount(); }

// Reduced motion is the one case where nothing plays by itself; hover is then
// the only way to see a video move at all, so the old handlers survive for it.
if (RM) {
  grid.addEventListener('mouseover',e=>{
    const box = e.target.closest('.thumb'); if(!box || box.contains(e.relatedTarget)) return;
    const v = box.querySelector('video'); if(v) setPlaying(v, true);
  });
  grid.addEventListener('mouseout',e=>{
    const box = e.target.closest('.thumb'); if(!box || box.contains(e.relatedTarget)) return;
    const v = box.querySelector('video'); if(v){ setPlaying(v, false); try{v.currentTime=0;}catch(_){} }
  });
}

grid.addEventListener('click',e=>{
  // Copying must not also toggle the card: the label sits inside it, so this
  // has to run first and stop there.
  const cp = e.target.closest('.copyable');
  if(cp){ e.stopPropagation(); copyText(cp.dataset.copy); return; }
  const card = e.target.closest('.card'); if(!card) return;
  const i = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(i < 0 || ITEMS[i].isLogo) return;   // preview-only card: not toggleable
  if(e.shiftKey && lastIdx!==null){
    const [a,b]=[Math.min(lastIdx,i),Math.max(lastIdx,i)];
    const val = !ITEMS[i].included;
    for(let k=a;k<=b;k++){ if(ITEMS[k].isLogo) continue; ITEMS[k].included=val; setCard(ITEMS[k]); }
  } else {
    ITEMS[i].included=!ITEMS[i].included; setCard(ITEMS[i]);
  }
  lastIdx=i; updateCount();
});

// --- Drag & drop reordering (sets the publish order) --------------------
let dragKey = null;
let orderTimer = null;
function saveOrder(){
  clearTimeout(orderTimer);
  orderTimer = setTimeout(async ()=>{
    const order = ITEMS.filter(x=>!x.isLogo).map(x=>x.key);
    try{
      const r = await fetch('/api/order',{method:'POST',
        headers:{'Content-Type':'application/json','X-Panel-Token':TOKEN},
        body:JSON.stringify({order})});
      toast(r.ok ? 'Order saved ✓' : 'Could not save order');
    }catch(_){ toast('Could not save order'); }
  }, 400);
}
grid.addEventListener('dragstart',e=>{
  const card=e.target.closest('.card'); if(!card){e.preventDefault();return;}
  const it = ITEMS.find(x=>x.key===card.dataset.key);
  if(!it || it.isLogo){ e.preventDefault(); return; }   // logo is fixed first
  dragKey=card.dataset.key; card.classList.add('drag');
  e.dataTransfer.effectAllowed='move';
  try{e.dataTransfer.setData('text/plain',dragKey);}catch(_){}
});
let overCard = null;
grid.addEventListener('dragover',e=>{
  if(dragKey===null) return;
  e.preventDefault(); e.dataTransfer.dropEffect='move';
  const card=e.target.closest('.card');
  if(card === overCard) return;
  if(overCard) overCard.classList.remove('over');
  const it = card && ITEMS.find(x=>x.key===card.dataset.key);
  overCard = (it && !it.isLogo) ? card : null;
  if(overCard) overCard.classList.add('over');
});
grid.addEventListener('drop',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  stopEdgeScroll();
  // A drop that is not ON a card is not an instruction. This used to fall back
  // to ITEMS.length-1, so releasing over a grid gap -- and the gaps between
  // cards are a large target -- silently threw the emoji to the very end.
  const card=e.target.closest('.card');
  if(!card){ endDrag(); return; }
  const from = ITEMS.findIndex(x=>x.key===dragKey);
  let to = ITEMS.findIndex(x=>x.key===card.dataset.key);
  const firstMovable = ITEMS.findIndex(x=>!x.isLogo);
  if(to < firstMovable) to = firstMovable;            // never before the logo
  if(from>=0 && to>=0 && to!==from){
    const [moved]=ITEMS.splice(from,1);
    ITEMS.splice(to,0,moved);
    // Move the one node instead of rebuilding every card (which would drop
    // every loaded thumbnail and lottie player and re-request them all).
    const node = cards.get(moved.key);
    const ref = cards.get(ITEMS[to+1] ? ITEMS[to+1].key : null);
    grid.insertBefore(node, ref || null);
    renumber();
    saveOrder();
  }
  endDrag();
});
grid.addEventListener('dragend',endDrag);

function endDrag(){
  dragKey=null;
  stopEdgeScroll();
  if(overCard){ overCard.classList.remove('over'); overCard=null; }
  grid.querySelectorAll('.card.drag').forEach(c=>c.classList.remove('drag'));
}

// --- Auto-scroll while dragging near an edge ----------------------------
// Without this the drag is trapped in the current viewport: with 200 cards
// there is no way to carry #200 up to #10, because the page will not follow
// the pointer. Speed rises the deeper into the edge band you go, so a nudge
// creeps and a hard push travels.
const EDGE_BAND = 100;      // px from the top/bottom edge where scrolling starts
const EDGE_MAX  = 42;       // px per frame at the very edge
let edgeSpeed = 0, edgeFrame = null;

function edgeScroll(y){
  const over = EDGE_BAND - y;                       // >0 once inside the top band
  const under = y - (innerHeight - EDGE_BAND);      // >0 once inside the bottom band
  const depth = over > 0 ? -over : (under > 0 ? under : 0);
  edgeSpeed = Math.max(-EDGE_MAX, Math.min(EDGE_MAX,
                       Math.round(depth / EDGE_BAND * EDGE_MAX)));
  if(edgeSpeed && edgeFrame === null) stepEdge();
}

function stepEdge(){
  edgeFrame = requestAnimationFrame(()=>{
    edgeFrame = null;
    // Guarded on dragKey as well: a drag that ends outside the window never
    // fires drop, and an unguarded loop would scroll the page forever.
    if(dragKey === null || !edgeSpeed) return;
    scrollBy(0, edgeSpeed);
    stepEdge();
  });
}

function stopEdgeScroll(){
  edgeSpeed = 0;
  if(edgeFrame !== null){ cancelAnimationFrame(edgeFrame); edgeFrame = null; }
}

// On the DOCUMENT, not the grid. Grid events bubble here anyway, and at the top
// of the window the pointer is over the sticky header -- where the grid's own
// handler never fires, which is exactly when scrolling up is wanted.
document.addEventListener('dragover',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  edgeScroll(e.clientY);
});
document.addEventListener('dragend',endDrag);
// A drop anywhere outside the grid: cancel rather than reorder by guesswork.
document.addEventListener('drop',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  if(!e.target.closest('#grid')) endDrag();
});

document.getElementById('all').onclick=()=>setAll(()=>true);
document.getElementById('none').onclick=()=>setAll(()=>false);
document.getElementById('inv').onclick=()=>setAll(x=>!x.included);
document.getElementById('anim').onclick=()=>{
  ANIM_ON = !ANIM_ON;
  try{ localStorage.setItem('animOn', ANIM_ON ? '1' : '0'); }catch(_){}
  applyAnim();
};
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
  try{
    const r = await fetch('/api/save',{method:'POST',
      headers:{'Content-Type':'application/json','X-Panel-Token':TOKEN},
      body:JSON.stringify({excluded})});
    const j = await r.json();
    toast(r.ok ? `Saved ✓  ${j.included} included · ${j.excluded} excluded`
               : `Save failed: ${j.error||r.status}`);
  }catch(_){ toast('Save failed'); }
};
async function copyText(text){
  if(!text) return;
  // The panel is served from a loopback host, which IS a secure context, so
  // the async clipboard API is normally available. It still REJECTS in real
  // situations -- "Document is not focused" is the one this hit in testing --
  // so a rejection falls through to execCommand rather than giving up.
  if(navigator.clipboard && window.isSecureContext){
    try{ await navigator.clipboard.writeText(text); toast('Copied ' + text); return; }
    catch(_){ /* fall through */ }
  }
  let ok = false;
  try{
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly','');
    ta.style.cssText = 'position:fixed;top:-1000px';
    document.body.appendChild(ta);
    ta.select(); ta.setSelectionRange(0, text.length);
    ok = document.execCommand('copy');
    ta.remove();
  }catch(_){ ok = false; }
  // Never claim a copy that did not happen: the user would paste whatever was
  // on the clipboard before and not know why it was wrong.
  toast(ok ? 'Copied ' + text : 'Could not copy — select and copy manually');
}

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
    # BEFORE setup_logging: the logger registers the literal values of
    # SECRET_ENV_KEYS so they can be masked wherever they appear, and it can only
    # register what is already in the environment. Loading .env afterwards -- as
    # this did, via _detect_bot_username() further down -- left any .env-only
    # credential unregistered for literal masking in the one process that serves
    # a browser UI. It also decides the log retention window.
    from build_pack import load_env
    load_env()
    setup_logging("panel")
    ap = argparse.ArgumentParser(description="Curate downloaded emoji before publishing.")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--preview-fps", type=int, default=PREVIEW_FPS,
                    help="Frame rate for animated previews. The grid can show "
                         "60+ cards at once and the browser decodes every frame "
                         "of each, so this is the main lever on how heavy the "
                         "panel feels (default: %(default)s).")
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

    # Per-run mutation token: a page on another origin can neither read it nor
    # attach it to a no-cors POST, so it cannot re-order or de-select the
    # catalog behind the user's back.
    token = secrets.token_urlsafe(24)
    handler = make_handler(view, by_key, db_path, token, args.preview_fps)

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
    raise SystemExit(record_exit_code(main()))
