"""The headless-Chromium harness the panel's browser suites share.

Extracted so a second suite can drive the real page without importing a module
that owns `test_*` methods -- doing that collects those tests twice rather than
sharing anything, which is the same rule that keeps `MutationGuard` whole in
`test_panel_guard.py`.

Nothing here is a TestCase and nothing here asserts. It owns the browser, the
temp catalog, the HTTP server and the page-level stubs; the suites own the
questions.
"""

from __future__ import annotations

from contextlib import closing
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

from emojikit import panel
from emojikit import panel_view
from emojikit.catalog import Catalog

TOKEN = "test-token-value"
VIEWPORT = {"width": 1200, "height": 900}
OPT_OUT = "NUMERA_EMOJI_MAPPER_NO_BROWSER_TESTS"
HOWTO = (f"the panel browser tests need playwright and Chromium:\n"
         f"    python -m pip install -r requirements-dev.txt\n"
         f"    python -m playwright install chromium\n"
         f"Set {OPT_OUT}=1 to skip them deliberately.")

if os.environ.get(OPT_OUT) == "1":
    raise unittest.SkipTest(f"{OPT_OUT}=1")

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError as exc:       # a bare ImportError reads as a typo
    raise ModuleNotFoundError(HOWTO) from exc

# Test scaffolding, injected before the page's own scripts. The fetch gate is
# what makes an out-of-order acknowledgement reproducible: without control of
# WHEN each reply lands, the interleaving that loses an arrangement happens
# once in a hundred runs and never in CI.
STUB = """
window.__net = {calls: [], pingOk: true, passthrough: false};
window.__rejections = [];
window.__plays = 0;
addEventListener('unhandledrejection', e => window.__rejections.push(String(e.reason)));
const _play = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function(){ window.__plays++; return _play.apply(this, arguments); };
const _fetch = window.fetch.bind(window);
window.fetch = function(u, o){
  const s = String(u);
  if(window.__net.passthrough) return _fetch(u, o);
  if(s.indexOf('/api/ping') >= 0){
    return window.__net.pingOk
      ? Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({ok: true})})
      : Promise.reject(new TypeError('offline'));
  }
  if(s.indexOf('/api/order') >= 0 || s.indexOf('/api/save') >= 0){
    const rec = {path: s, body: JSON.parse(o.body), settled: false};
    rec.p = new Promise((res, rej) => { rec.res = res; rec.rej = rej; });
    window.__net.calls.push(rec);
    return rec.p;
  }
  return _fetch(u, o);
};
window.__sent = (kind) => window.__net.calls
  .map((c, i) => ({i: i, path: c.path, body: c.body, settled: c.settled}))
  .filter(c => !kind || c.path.indexOf(kind) >= 0);
window.__settle = (i, status) => {
  const rec = window.__net.calls[i];
  rec.settled = true;
  if(status === 0) rec.rej(new TypeError('network'));
  else rec.res({ok: status < 300, status: status,
                json: () => Promise.resolve(
                  {ok: status < 300, count: (rec.body.order || []).length,
                   included: 1, excluded: 1, error: 'refused'})});
};
// The exact pair of statements a drop runs: the model moves, then the order is
// queued. Driving them directly is what makes an interleaving deterministic;
// `test_F01_a_real_drag...` proves the same path through the real gesture.
window.__reorder = (from, to) => {
  carried.clear(); carried.add(ITEMS[from].key); moveCarried(to); carried.clear(); saveOrder();
};
window.__unload = () => {
  const e = new Event('beforeunload', {cancelable: true});
  dispatchEvent(e);
  return e.defaultPrevented;
};
window.__topRow = () => {
  let r = rowAt(scrollY + headerH - gridTop);
  while(r < rows.length && rows[r].sep) r++;
  return r < rows.length ? {start: rows[r].start, end: rows[r].end} : null;
};
window.__anchor = () => ({
  idx: firstVisibleIndex(),
  cols: G.cols,
  top: window.__topRow(),
  atEnd: Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight - 1,
});
// A real flick keeps firing scroll events, which keeps the freeze window open;
// one scrollBy closes it in 180 ms, long before an observer round trip.
window.__keepScrolling = (ms) => {
  const end = performance.now() + ms;
  const step = () => { scrollBy(0, 50);
                       if(performance.now() < end) requestAnimationFrame(step); };
  step();
};
window.__animating = () => [...document.querySelectorAll('#grid img[data-anim]')]
  .filter(n => n.getAttribute('src') === n.dataset.anim).length;
window.__mounted = () => document.querySelectorAll('#grid img[data-anim]').length;
// Selection state, for the queue suite: what the page shows, what it has been
// told the server holds, and whether it is advertising the difference.
window.__excluded = () => ITEMS.filter(x => !x.isLogo && !x.included).map(x => x.key);
window.__dirty = () => selDirty();
window.__dirtyShown = () =>
  document.getElementById('save').classList.contains('dirty');
window.__toggle = (i) => { ITEMS[i].included = !ITEMS[i].included;
                           setCard(ITEMS[i]); updateCount(); markSelDirty(); };
window.__save = () => document.getElementById('save').click();
"""

# Denied site data. Chrome throws on the PROPERTY, not just on getItem, when a
# profile blocks storage -- which is why an unguarded read at module scope took
# the rest of the file with it.
DENY_STORAGE = """
const boom = () => { throw new DOMException('site data is blocked', 'SecurityError'); };
Object.defineProperty(window, 'localStorage', {configurable: true, get: boom});
"""


class Harness:
    """One browser, one served panel, for the life of a test class."""

    def __init__(self, catalog_size: int = 4):
        self.catalog_size = catalog_size
        self.browser = None
        self._pw = None

    def start(self) -> None:
        self._pw = sync_playwright().start()
        try:
            # Local hardware checks may use installed Chrome; CI keeps its pinned build.
            channel = os.environ.get("NUMERA_EMOJI_MAPPER_BROWSER_CHANNEL") or None
            self.browser = self._pw.chromium.launch(headless=True, channel=channel)
        except Exception as exc:         # re-raised, with the cure attached
            self._pw.stop()
            raise RuntimeError(HOWTO) from exc

        # Windows test artifacts stay under the project; CI uses the same layout.
        temp_root = ROOT / "logs" / "test-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.data = Path(self.tmp.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(self.catalog_size):
                img = self.data / "media" / "static" / f"i{i}.png"
                img.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (40, 40), (9 * i % 256, 20, 30, 255)).save(img, "PNG")
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        keywords=[f"item{i}"])
            view, by_key, hidden = panel.build_view(cat, "")
        self.png = (self.data / "media" / "static" / "i0.png").read_bytes()
        handler = panel.make_handler(view, by_key, self.db, TOKEN, hidden=hidden)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        alive = self.thread.is_alive()
        self.httpd.server_close()
        self.tmp.cleanup()
        if self.browser is not None:
            self.browser.close()
        if self._pw is not None:
            self._pw.stop()
        if alive:
            raise AssertionError("panel server thread leaked")

    def open(self, items=None, *, init=None, clock=False, on_error=None,
             cleanup=None, **ctx_args):
        """The served page, with an optional synthetic model."""
        ctx = self.browser.new_context(viewport=VIEWPORT, **ctx_args)
        if cleanup is not None:
            cleanup(ctx.close)
        page = ctx.new_page()
        if on_error is not None:
            page.on("pageerror", lambda e: on_error(str(e)))
        if clock:
            page.clock.install()
        page.add_init_script(STUB)
        if init:
            page.add_init_script(init)
        if items is not None:
            page.route(self.url, self._rewrite(items))
        # Media is not what any of these test, and a synthetic model would
        # otherwise 404 once per card against the real server.
        for pattern in ("**/img/**", "**/preview/**"):
            page.route(pattern, lambda r: r.fulfill(
                status=200, content_type="image/png", body=self.png))
        page.goto(self.url, wait_until="load", timeout=30_000)
        return page

    def _rewrite(self, items):
        payload = panel._json_for_script(items)

        def handler(route):
            served = route.fetch()
            html = re.sub(
                r'(<script id="items-data" type="application/json">).*?(</script>)',
                lambda m: m.group(1) + payload + m.group(2), served.text(), flags=re.S)
            route.fulfill(status=200, content_type="text/html; charset=utf-8",
                          body=html)
        return handler

    def db_order(self):
        # Observe committed WAL state while the server writes. Constructing a
        # Catalog owns the writer lease and would make the observer contend.
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as con:
            return [r[0] for r in con.execute("SELECT content_key FROM items ORDER BY position, rowid")]

    def excluded_in_db(self):
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as con:
            return {r[0] for r in con.execute("SELECT content_key FROM items WHERE included=0")}


def synth(n, *, logo=False, fmt="static", packs=None, excluded=()):
    """A model in the exact shape ``panel_view.build_view`` produces.

    Four hundred rows of real catalog would cost four hundred PNGs and four
    hundred phash comparisons to prove arithmetic that never touches the
    database. The page, its scripts and its CSS are the real ones; only the
    catalog behind them is a fixture.
    """
    out = []
    if logo:
        out.append({"key": panel_view.LOGO_KEY, "fmt": "static", "isLogo": True,
                    "label": "Brand logo (auto-added on publish)", "emoji": "",
                    "included": True})
    for i in range(n):
        card = {"key": f"x:{i:030d}", "fmt": fmt, "label": f"item {i}",
                "copyId": None, "emoji": "", "included": i not in excluded}
        if packs is not None:
            card["pack"] = packs[i]
        out.append(card)
    return out
