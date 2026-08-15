"""Tests for the curate panel's brand-logo preview (panel.build_view).

The brand logo is never part of the catalog (it's injected only at publish
time by build_collection.py), but the panel should still show a preview card
for it -- without letting it affect the real included/excluded counts or be
sent to /api/save. These tests lock down that separation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import panel as p  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (40, 40), (10, 20, 30, 255)).save(path, "PNG")


class BrandLogoPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(3):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            self.cat_path = self.data / "catalog.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _view(self, bot_username: str, logo_file: Path | None):
        target = str(logo_file) if logo_file else p.BRAND_LOGO_DEFAULT
        with mock.patch.object(p, "BRAND_LOGO_DEFAULT", target):
            with Catalog(self.cat_path) as cat:
                return p.build_view(cat, bot_username)

    def test_logo_shown_first_for_emoji_mapper_bot(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, by_key = self._view("GodVerifyEmojiMapperbot", logo)
        self.assertTrue(view[0]["isLogo"])
        self.assertEqual(view[0]["key"], p.LOGO_KEY)
        self.assertEqual(by_key[p.LOGO_KEY], logo)
        # The 3 real catalog items still follow, none marked as logo.
        self.assertEqual(len(view), 4)
        self.assertTrue(all(not v.get("isLogo") for v in view[1:]))

    def test_logo_hidden_for_coin_bot(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, _ = self._view("GodVerifyCoinEmojiMapperbot", logo)
        self.assertEqual(len(view), 3)  # no logo card injected
        self.assertTrue(all(not v.get("isLogo") for v in view))

    def test_logo_hidden_when_file_missing(self):
        missing = self.data / "does_not_exist.png"
        view, _ = self._view("GodVerifyEmojiMapperbot", missing)
        self.assertEqual(len(view), 3)

    def test_logo_hidden_when_bot_unknown(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, _ = self._view("", logo)
        self.assertEqual(len(view), 3)


class InertItemJson(unittest.TestCase):
    """A catalog label must never be able to escape the data block."""

    def test_script_close_is_escaped(self):
        out = p._json_for_script([{"label": "</script><script>alert(1)</script>"}])
        self.assertNotIn("</script>", out)
        self.assertNotIn("<", out)
        self.assertIn("\\u003c", out)

    def test_js_line_separators_are_escaped(self):
        out = p._json_for_script([{"label": "a b c"}])
        self.assertNotIn(" ", out)
        self.assertNotIn(" ", out)

    def test_roundtrips(self):
        items = [{"key": "s:1", "label": "ok <b>", "included": True}]
        self.assertEqual(json.loads(p._json_for_script(items)), items)


class LoopbackCheck(unittest.TestCase):
    def test_accepts_loopback_forms(self):
        for h in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1",
                  "http://127.0.0.1:8765", "[::1]:8765"):
            self.assertTrue(p._is_loopback(h), h)

    def test_rejects_foreign_hosts(self):
        for h in ("evil.com", "evil.com:8765", "http://evil.com",
                  "127.0.0.1.evil.com", ""):
            self.assertFalse(p._is_loopback(h), h)


class MutationGuard(unittest.TestCase):
    """POST routes must reject anything a hostile page could actually send."""

    TOKEN = "test-token-value"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.db = data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(2):
                img = data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            self.view, by_key = p.build_view(cat, "")

        handler = p.make_handler(self.view, by_key, self.db, self.TOKEN)
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

    def _post(self, path, body, *, token=TOKEN, ctype="application/json",
              origin=None, length=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = request.Request(f"http://127.0.0.1:{self.port}{path}", data=raw,
                              method="POST")
        if ctype:
            req.add_header("Content-Type", ctype)
        if token is not None:
            req.add_header("X-Panel-Token", token)
        if origin:
            req.add_header("Origin", origin)
        if length is not None:
            req.add_header("Content-Length", str(length))
        try:
            with request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _keys(self):
        return [v["key"] for v in self.view if not v.get("isLogo")]

    def test_missing_token_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": []}, token=None)
        self.assertEqual(code, 403)

    def test_wrong_token_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": []}, token="nope")
        self.assertEqual(code, 403)

    def test_non_json_content_type_is_rejected(self):
        # The exact shape a cross-origin no-cors POST can send.
        code, _ = self._post("/api/save", {"excluded": []}, ctype="text/plain")
        self.assertEqual(code, 403)

    def test_foreign_origin_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": []}, origin="http://evil.com")
        self.assertEqual(code, 403)

    def test_malformed_json_is_400(self):
        code, _ = self._post("/api/save", b"{not json")
        self.assertEqual(code, 400)

    def test_unknown_keys_rejected(self):
        code, _ = self._post("/api/save", {"excluded": [], "wat": 1})
        self.assertEqual(code, 400)

    def test_order_must_be_a_permutation(self):
        keys = self._keys()
        code, _ = self._post("/api/order", {"order": keys[:1]})
        self.assertEqual(code, 400, "a partial order would silently drop items")
        code, _ = self._post("/api/order", {"order": keys + ["s:bogus"]})
        self.assertEqual(code, 400)

    def test_valid_requests_still_work(self):
        keys = self._keys()
        code, body = self._post("/api/order", {"order": list(reversed(keys))})
        self.assertEqual((code, body["ok"]), (200, True))
        code, body = self._post("/api/save", {"excluded": [keys[0]]})
        self.assertEqual(code, 200)
        self.assertEqual(body["excluded"], 1)


if __name__ == "__main__":
    unittest.main()
