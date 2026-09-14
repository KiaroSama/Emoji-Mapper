"""The panel's POST guard, and the two behaviours built on top of it.

``MutationGuard`` owns nine ``test_*`` methods and is subclassed twice, so all
three classes must stay in ONE module: importing the base somewhere else would
collect it again and inflate the suite rather than move it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

from emojikit import panel as p
from emojikit import panel as p_mod
from emojikit.catalog import Catalog


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
            self.view, by_key, _hidden = p.build_view(cat, "")

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

    def _get(self, path, *, host=None):
        req = request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        if host:
            req.add_header("Host", host)
        try:
            with request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except error.HTTPError as e:
            return e.code, e.read()

    def _keys(self):
        return [v["key"] for v in self.view if not v.get("isLogo")]

    def test_missing_token_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": [], "known": []}, token=None)
        self.assertEqual(code, 403)

    def test_wrong_token_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": [], "known": []}, token="nope")
        self.assertEqual(code, 403)

    def test_non_json_content_type_is_rejected(self):
        # The exact shape a cross-origin no-cors POST can send.
        code, _ = self._post("/api/save", {"excluded": [], "known": []}, ctype="text/plain")
        self.assertEqual(code, 403)

    def test_foreign_origin_is_rejected(self):
        code, _ = self._post("/api/save", {"excluded": [], "known": []}, origin="http://evil.com")
        self.assertEqual(code, 403)

    def test_malformed_json_is_400(self):
        code, _ = self._post("/api/save", b"{not json")
        self.assertEqual(code, 400)

    def test_unknown_keys_rejected(self):
        code, _ = self._post("/api/save", {"excluded": [], "known": [], "wat": 1})
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
        code, body = self._post("/api/save", {"excluded": [keys[0]], "known": keys})
        self.assertEqual(code, 200)
        self.assertEqual(body["excluded"], 1)

    def test_get_rejects_a_foreign_host(self):
        """A page on a hostname that resolves to loopback is same-origin here.

        The 127.0.0.1 bind stops remote sockets, not that page, and "/" carries
        the per-run mutation token in its body -- the one secret the POST guard
        rests on. Reads need the Host check the writes already had.
        """
        for path in ("/", "/img/" + self._keys()[0]):
            with self.subTest(path=path):
                self.assertEqual(self._get(path, host="evil.com")[0], 403)
                self.assertEqual(self._get(path)[0], 200, "loopback still works")


class LosingTheServerIsNeverSilent(MutationGuard):
    """An owner reordered a pack for three hours against a dead panel.

    The page looked fine, every drag "worked", nothing reached the catalog, and
    the only signal was a toast that fades in 2.6 seconds. These pin the parts
    that make that impossible to miss.
    """

    def test_ping_answers_without_a_token_and_touches_nothing(self):
        """The page polls this constantly; it must be cheap and unguarded.

        A liveness probe that needed the mutation token could not distinguish
        "server gone" from "token stale", and one that took the catalog lock
        would report a healthy panel as dead while a publish held it.
        """
        with request.urlopen(f"http://127.0.0.1:{self.port}/api/ping",
                             timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read()), {"ok": True})

    def test_a_rejected_order_is_an_error_status_not_a_200(self):
        """The page decides success from the HTTP status.

        A 200 carrying {"ok": false} would be reported to the owner as
        "Order saved ✓" while nothing was written.
        """
        status, _ = self._post("/api/order", {"order": ["s:" + "0" * 30]})
        self.assertEqual(status, 400)

    # The behaviour lives in the served scripts (panel.SCRIPT); the page itself
    # (panel.PAGE) carries only the markup the scripts act on.

    def test_the_page_polls_and_keeps_the_warning_up(self):
        script = p.SCRIPT
        self.assertIn("/api/ping", script)
        self.assertIn("setInterval(", script)
        # A banner, not a toast: the toast auto-hides after 2600ms.
        self.assertIn("id=\"alert\"", p.PAGE)
        self.assertNotIn("setTimeout(()=>a.classList.remove('show')", script)

    def test_unsaved_work_is_remembered_and_retried(self):
        script = p.SCRIPT
        self.assertIn("pendingOrder", script)
        # The heartbeat flushes it, so recovery needs no action from the owner.
        beat = script[script.index("setInterval(async ()=>{"):]
        self.assertIn("flushOrder(pendingOrder)", beat[:600])

    def test_a_restarted_panel_does_not_strand_the_page(self):
        """The token is per run, so a restart 403s the page's saves.

        Re-reading it from "/" is same-origin -- exactly the boundary the token
        protects -- so this weakens nothing, and it is the difference between
        "restart the panel and lose your afternoon" and "it catches up".
        """
        script = p.SCRIPT
        api = script[script.index("async function apiPost("):]
        body = api[:api.index("async function flushOrder")]
        self.assertIn("r.status === 403", body)
        self.assertIn("const TOKEN =", body)     # re-parsed from a fresh "/"

    def test_the_browser_asks_before_closing_on_unsaved_work(self):
        script = p.SCRIPT
        self.assertIn("beforeunload", script)
        guard = script[script.index("addEventListener('beforeunload'"):]
        self.assertIn("if(pendingOrder)", guard[:200])


class RefreshMustActuallyRefresh(MutationGuard):
    """Reloading the page has to show the current catalog, not a snapshot.

    ``view`` was built once at start-up, so anything that changed the catalog
    afterwards -- fetch_emoji_ids.py adding an emoji, add_media.py ingesting a
    folder -- stayed invisible until the panel was restarted, and a refresh
    looked like it did nothing.
    """

    def _page_items(self):
        with request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            html = r.read().decode()
        raw = re.search(r'<script id="items-data" type="application/json">(.*?)</script>',
                        html, re.S).group(1)
        return json.loads(raw.replace("\u003c", "<").replace("\u003e", ">"))

    def test_a_reload_sees_a_change_another_process_made(self):
        before = self._page_items()
        key = before[0]["key"]

        con = sqlite3.connect(self.db)
        con.execute("UPDATE items SET keywords=? WHERE content_key=?",
                    (json.dumps(["premium-id:9999999999999999999"]), key))
        con.commit()
        con.close()

        after = self._page_items()
        self.assertNotEqual(before[0]["label"], after[0]["label"],
                            "the page still served the start-up snapshot")
        self.assertEqual(after[0]["copyId"], "9999999999999999999",
                         "derived fields must be rebuilt too, not just carried")

    def test_the_reload_replaces_the_shared_view_in_place(self):
        """Rebinding it would leave every route closed over the old object."""
        src = Path(p_mod.__file__).read_text(encoding="utf-8")
        block = src[src.index("def _reload_view("):]
        block = block[:block.index("class Handler")]
        self.assertIn("view[:] = fresh", block)
        self.assertIn("by_key.clear()", block)
        self.assertNotIn("view = fresh", block)


if __name__ == "__main__":
    unittest.main()
