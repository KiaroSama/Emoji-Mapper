"""A tab may only decide the inclusion of emoji it could actually see.

From the audit's adjacent-path list. `POST /api/save` carries the FULL
selection -- every key it does not name becomes included -- so a request with
no notion of scope speaks for the whole catalog, including rows the page has
never heard of.

That is not hypothetical. The panel re-reads the catalog on every page load, so
a tab opened before `fetch_emoji_ids.py` added an emoji, or before the owner
deselected one in a second tab, holds a stale snapshot. Saving from it re-
included the rows it had never seen and answered `{"ok": true}`.

The request now says what it was showing, and the server applies the decision
only inside that scope; everything outside keeps whatever the catalog holds. A
page too old to say is refused, because one reload fixes that while a silent
re-inclusion is invisible until a publish ships the wrong pack.

Driven through the real handler on a real socket: the bug lives in the contract
between the page and the server, and only both halves together can show it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

import panel as p
from emojikit.catalog import Catalog

TOKEN = "scope-test-token"


class SaveScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(3):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=self.key(i), fmt="static", file_path=img,
                        emojis=["\U0001f600"], keywords=[f"item{i}"])
            view, by_key, hidden = p.build_view(cat, "")
        handler = p.make_handler(view, by_key, self.db, TOKEN)
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

    @staticmethod
    def key(i: int) -> str:
        return f"s:item{i:030d}"

    def post(self, body):
        req = request.Request(f"http://127.0.0.1:{self.port}/api/save",
                              data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Panel-Token", TOKEN)
        try:
            with request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def excluded_now(self):
        with Catalog(self.db) as cat:
            return {it.content_key for it in cat.all_items() if not it.included}

    def test_an_ordinary_in_scope_save_still_works(self):
        scope = [self.key(i) for i in range(3)]
        code, body = self.post({"excluded": [self.key(1)], "known": scope})
        self.assertEqual(code, 200)
        self.assertEqual(body["excluded"], 1)
        self.assertEqual(self.excluded_now(), {self.key(1)})

    def test_an_in_scope_key_can_be_re_included(self):
        scope = [self.key(i) for i in range(3)]
        self.post({"excluded": [self.key(1)], "known": scope})
        code, _ = self.post({"excluded": [], "known": scope})
        self.assertEqual(code, 200)
        self.assertEqual(self.excluded_now(), set())

    def test_a_key_outside_the_scope_keeps_its_exclusion(self):
        """The reproduction. item2 is deselected; a tab that only ever saw
        item0 and item1 saves, and must not speak for item2."""
        scope = [self.key(i) for i in range(3)]
        self.post({"excluded": [self.key(2)], "known": scope})
        self.assertEqual(self.excluded_now(), {self.key(2)})

        stale_scope = [self.key(0), self.key(1)]
        code, _body = self.post({"excluded": [], "known": stale_scope})
        self.assertEqual(code, 200)
        self.assertEqual(self.excluded_now(), {self.key(2)},
                         "a stale tab re-included an emoji it never saw")

    def test_a_stale_tab_cannot_re_include_by_omission(self):
        """Same shape, stated the other way round: omission from `excluded` is
        only meaningful for keys the tab was actually showing."""
        scope = [self.key(i) for i in range(3)]
        self.post({"excluded": [self.key(0), self.key(2)], "known": scope})
        self.assertEqual(self.excluded_now(), {self.key(0), self.key(2)})

        code, _ = self.post({"excluded": [self.key(0)],
                             "known": [self.key(0), self.key(1)]})
        self.assertEqual(code, 200)
        self.assertEqual(self.excluded_now(), {self.key(0), self.key(2)})

    def test_a_page_that_cannot_say_what_it_saw_is_refused(self):
        """An older page posts no scope at all. Guessing on its behalf is what
        loses a selection silently; a 409 costs one reload."""
        self.post({"excluded": [self.key(2)],
                   "known": [self.key(i) for i in range(3)]})
        before = self.excluded_now()
        code, body = self.post({"excluded": []})
        self.assertEqual(code, 409)
        self.assertIn("reload", body["error"])
        self.assertEqual(self.excluded_now(), before,
                         "a refused save still changed the catalog")

    def test_a_malformed_scope_is_refused_the_same_way(self):
        for bad in ("everything", 7, [1, 2], {"a": 1}):
            with self.subTest(known=bad):
                code, _ = self.post({"excluded": [], "known": bad})
                self.assertEqual(code, 409)

    def test_a_scope_naming_unknown_keys_is_harmless(self):
        """A tab can hold a key the catalog no longer has -- an item removed
        between loads. It is intersected away, not treated as an instruction."""
        code, _ = self.post({"excluded": [],
                             "known": [self.key(0), "s:vanished", "nonsense"]})
        self.assertEqual(code, 200)

    def test_an_unknown_field_is_still_rejected(self):
        code, body = self.post({"excluded": [], "known": [], "wat": 1})
        self.assertEqual(code, 400)
        self.assertIn("unknown keys", body["error"])


if __name__ == "__main__":
    unittest.main()
