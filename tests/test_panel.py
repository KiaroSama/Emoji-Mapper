"""Tests for the curate panel's brand-logo preview (panel.build_view).

The brand logo is never part of the catalog (it's injected only at publish
time by build_collection.py), but the panel should still show a preview card
for it -- without letting it affect the real included/excluded counts or be
sent to /api/save. These tests lock down that separation.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import panel as p  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402
from emojikit.media import hamming  # noqa: E402


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


class _WatchedLock:
    """A lock that announces every acquisition *attempt*.

    That signal is the whole synchronisation point of the test below: the panel
    reads ``view`` either just before this fires (the defect) or just after it
    (the fix), so the test never has to wait on something that must not happen.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.attempted = threading.Event()

    def __enter__(self):
        self.attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


class _Tripwire(dict):
    """A view entry that suspends whatever is reading the view mid-``sort()``.

    ``list.sort()`` empties the list *before* calling the key function, so a
    key call that finds the list empty is running inside the sort -- exactly
    the window the concurrent save has to land in.
    """

    def __init__(self, item, view, entered, release):
        super().__init__(item)
        self._view, self._entered, self._release = view, entered, release

    def get(self, key, *default):
        if key == "isLogo" and not self._view:
            self._entered.set()
            self._release.wait(timeout=30)
        return super().get(key, *default)


class SaveDuringReorder(unittest.TestCase):
    """A save landing mid-``view.sort()`` must not wipe the user's curation.

    ``/api/order`` sorts ``view`` in place and CPython empties a list for the
    duration of ``list.sort()``. Reading ``view`` outside the lock therefore saw
    zero known keys, intersected the posted exclusions down to nothing, and
    ``set_inclusion(set())`` re-included every row -- while still answering
    ``{"ok": true}``. The panel debounces its order POST by 400 ms, so "drag,
    then hit Save" is the ordinary way to land in that window.

    The interleaving is pinned, not raced: the reorder is suspended inside
    ``sort()``, and the save is only released once it has reached the lock.
    """

    TOKEN = "test-token-value"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.db = data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(4):
                img = data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            self.view, by_key = p.build_view(cat, "")

        self.sorting = threading.Event()
        self.release = threading.Event()
        self.view[0] = _Tripwire(self.view[0], self.view, self.sorting, self.release)

        self.lock = _WatchedLock()
        with mock.patch.object(threading, "Lock", lambda: self.lock):
            handler = p.make_handler(self.view, by_key, self.db, self.TOKEN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()          # never leave a suspended request behind
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "panel server thread leaked")
        self.httpd.server_close()
        self.tmp.cleanup()

    def _post(self, path, body):
        req = request.Request(f"http://127.0.0.1:{self.port}{path}",
                              data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Panel-Token", self.TOKEN)
        try:
            with request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_exclusions_survive_a_concurrent_reorder(self):
        keys = [v["key"] for v in self.view]
        excluded = keys[:2]
        results: dict = {}

        def run(name, path, body):
            results[name] = self._post(path, body)

        reorder = threading.Thread(target=run, args=(
            "order", "/api/order", {"order": list(reversed(keys))}), daemon=True)
        save = threading.Thread(target=run, args=(
            "save", "/api/save", {"excluded": excluded}), daemon=True)

        reorder.start()
        self.assertTrue(self.sorting.wait(timeout=30), "reorder never reached sort")
        # The reorder is now inside sort() with the view emptied. Anything the
        # save reads from here until release() is the corrupted snapshot.
        self.lock.attempted.clear()
        save.start()
        self.assertTrue(self.lock.attempted.wait(timeout=30),
                        "the save never reached the lock")
        self.release.set()
        for t in (reorder, save):
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "request thread leaked")

        self.assertEqual(results["order"][0], 200)
        self.assertEqual(results["save"][0], 200)
        self.assertEqual(results["save"][1]["excluded"], len(excluded),
                         "the save reported a count it did not persist")
        with Catalog(self.db) as cat:
            still_out = {it.content_key for it in cat.all_items() if not it.included}
        self.assertEqual(still_out, set(excluded),
                         "the de-selection was discarded by the concurrent sort")

    def test_the_page_is_not_served_from_an_emptied_view(self):
        """Serialising ``view`` unlocked during a sort rendered an empty grid."""
        keys = [v["key"] for v in self.view]
        page: dict = {}

        def fetch():
            with request.urlopen(
                    f"http://127.0.0.1:{self.port}/", timeout=30) as r:
                page["body"] = r.read().decode()

        reorder = threading.Thread(target=lambda: self._post(
            "/api/order", {"order": list(reversed(keys))}), daemon=True)
        reorder.start()
        self.assertTrue(self.sorting.wait(timeout=30), "reorder never reached sort")
        self.lock.attempted.clear()
        getter = threading.Thread(target=fetch, daemon=True)
        getter.start()
        # Fast on a correct handler (it takes the lock immediately); only a
        # handler that skipped the lock entirely ever waits this out.
        self.assertTrue(self.lock.attempted.wait(timeout=10),
                        "do_GET read the view without taking the lock")
        self.release.set()
        for t in (reorder, getter):
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "request thread leaked")

        for k in keys:
            self.assertIn(k, page["body"], "the grid was served without its items")


class CatalogUnavailable(unittest.TestCase):
    """A busy/unopenable catalog must produce an answer, not a dropped socket.

    build_collection.py reads the same database file, so sqlite3.Error is
    routine here. Uncaught it escaped do_POST and closed the connection with no
    HTTP response at all, leaving the panel with nothing to report.
    """

    TOKEN = "test-token-value"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.db = data / "catalog.db"
        with Catalog(self.db) as cat:
            img = data / "media" / "static" / "i0.png"
            _make_png(img)
            cat.add(content_key="s:item0", fmt="static", file_path=img)
            self.view, by_key = p.build_view(cat, "")

        # Replace the database with a directory: sqlite3 then refuses to open
        # it, which is a real sqlite3.Error on the same code path a locked
        # database takes, without a five-second busy wait.
        for f in data.glob("catalog.db*"):
            f.unlink()
        self.db.mkdir()

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

    def _post(self, path, body):
        req = request.Request(f"http://127.0.0.1:{self.port}{path}",
                              data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Panel-Token", self.TOKEN)
        try:
            with request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_save_answers_503(self):
        code, body = self._post("/api/save", {"excluded": ["s:item0"]})
        self.assertEqual(code, 503)
        self.assertIn("catalog unavailable", body["error"])

    def test_order_answers_503(self):
        code, body = self._post("/api/order", {"order": ["s:item0"]})
        self.assertEqual(code, 503)
        self.assertIn("catalog unavailable", body["error"])


@dataclass
class _Fake:
    fmt: str
    phash: int | None


def _reference_order(items: list) -> list:
    """The original greedy walk, written against ``media.hamming``."""
    out: list = []
    for fmt in sorted({it.fmt for it in items}, key=lambda f: p.FMT_ORDER.get(f, 9)):
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


class SimilarityOrder(unittest.TestCase):
    """The look-alike grouping must not shift when the walk gets faster.

    ``order_by_similarity`` seeds the saved publish order, so a different
    ordering is a different pack. The inlined popcount is only allowed to be
    media.hamming's exact result, first-minimum tie-break included.
    """

    def _items(self, seed: int) -> list:
        rnd = random.Random(seed)
        items = [_Fake("static", rnd.getrandbits(64)) for _ in range(150)]
        # Duplicate hashes exercise the distance-0 shortcut, and equal distances
        # exercise the tie-break: min() keeps the FIRST minimum.
        items += [_Fake("static", items[3].phash) for _ in range(6)]
        items += [_Fake("video", rnd.getrandbits(64) & 0xFF) for _ in range(60)]
        items += [_Fake("animated", None) for _ in range(4)]
        rnd.shuffle(items)
        return items

    def test_matches_the_reference_walk(self):
        for seed in (1, 1234, 99999):
            with self.subTest(seed=seed):
                items = self._items(seed)
                self.assertEqual(
                    [id(x) for x in p.order_by_similarity(items)],
                    [id(x) for x in _reference_order(items)])

    def test_unhashed_items_follow_their_format_group(self):
        items = [_Fake("animated", None), _Fake("static", 1), _Fake("static", 2)]
        out = p.order_by_similarity(items)
        self.assertEqual([it.fmt for it in out], ["static", "static", "animated"])


if __name__ == "__main__":
    unittest.main()
