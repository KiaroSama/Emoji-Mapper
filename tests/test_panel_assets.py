"""The panel's behaviour ships as several script files, versioned by their content.

The page moved its JavaScript out of the HTML asset and into `/static/`, which
is immutable-cached. Without a version in the URL an edited script would keep
being served stale from the browser cache -- the panel would look unchanged
after a fix, exactly the way it once did when the server itself held a stale
snapshot. So the version IS the content, and the route has to accept it.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

from emojikit import panel as p
from emojikit.catalog import Catalog


class ThePanelScriptsShip(unittest.TestCase):
    def test_every_script_is_a_real_file_inside_the_repo(self):
        """Same guard as the page itself: an absolute path would make the
        behaviour silently vanish on every machine but one."""
        for name in p.SCRIPT_FILES:
            asset = p.ASSET_DIR / name
            self.assertTrue(asset.is_file(), f"missing script: {asset}")
            self.assertTrue(str(asset.resolve()).startswith(str(ROOT.resolve())))

    def test_the_page_loads_every_script_in_order_and_versioned(self):
        page = p.PAGE
        tags = [f'<script src="/static/{n}?v=__ASSET_VER__"></script>' for n in p.SCRIPT_FILES]
        for tag in tags:
            self.assertIn(tag, page, tag)
        # The grid must exist before the gestures that act on it, whatever
        # comes after -- each file only needs what loaded before it.
        for a, b in zip(tags, tags[1:], strict=False):  # deliberately different lengths
            self.assertLess(page.index(a), page.index(b))
        # All of them come AFTER the inert data block they parse.
        self.assertLess(page.index('<script id="items-data"'), page.index(tags[0]))

    def test_the_version_is_the_scripts_content(self):
        self.assertEqual(p.ASSET_VER,
                         hashlib.sha1(p.SCRIPT.encode("utf-8")).hexdigest()[:12])
        # A concatenation of every file, so a change to either moves the version.
        for name in p.SCRIPT_FILES:
            self.assertIn((p.ASSET_DIR / name).read_text(encoding="utf-8"), p.SCRIPT)

    def test_the_icon_url_is_versioned_by_the_icon_itself(self):
        """The icon is immutable-cached like the scripts. Unversioned, a
        replaced logo kept showing the OLD one from every browser that had
        opened the panel before."""
        self.assertEqual(p.ICON_VER, hashlib.sha1(
            (p.ASSET_DIR / "logo-128.png").read_bytes()).hexdigest()[:12])
        self.assertNotIn('"/static/logo-128.png"', p.PAGE)
        self.assertEqual(p.PAGE.count("/static/logo-128.png?v=__ICON_VER__"), 2)

    def test_the_inline_block_carries_values_not_behaviour(self):
        """Everything that used to be inline now lives in the files, so a
        handler left behind in the page would run twice or against nothing."""
        inline = p.PAGE[p.PAGE.index('<script id="items-data"'):]
        self.assertNotIn("addEventListener", inline)
        self.assertNotIn("function ", inline)
        for token in ("__TOKEN__", "__PREVIEW_FPS__", "__PER_SET__", "__HIDDEN__"):
            self.assertIn(token, inline)


class TheStaticRouteServesVersionedScripts(unittest.TestCase):
    TOKEN = "test-token-value"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.db = data / "catalog.db"
        with Catalog(self.db) as cat:
            img = data / "media" / "static" / "i0.png"
            _make_png(img)
            cat.add(content_key="s:item0", fmt="static", file_path=img)
            view, by_key, _hidden = p.build_view(cat, "")
        handler = p.make_handler(view, by_key, self.db, self.TOKEN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "panel server thread leaked")
        self.httpd.server_close()
        self.tmp.cleanup()

    def _get(self, path):
        with request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()

    def test_the_served_page_names_the_current_version(self):
        _status, _headers, body = self._get("/")
        html = body.decode("utf-8")
        self.assertNotIn("__ASSET_VER__", html, "the placeholder reached the browser")
        self.assertIn(f"/static/{p.SCRIPT_FILES[0]}?v={p.ASSET_VER}", html)
        self.assertNotIn("__ICON_VER__", html, "the icon placeholder reached the browser")
        self.assertIn(f"/static/logo-128.png?v={p.ICON_VER}", html)

    def test_the_version_query_reaches_the_file_on_disk(self):
        """`?v=` is for the cache, not the filesystem: the route must strip it
        or every versioned URL is a 404 and the page has no behaviour at all."""
        status, headers, body = self._get(f"/static/{p.SCRIPT_FILES[0]}?v={p.ASSET_VER}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript")
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(body, (p.ASSET_DIR / p.SCRIPT_FILES[0]).read_bytes())


if __name__ == "__main__":
    unittest.main()
