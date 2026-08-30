"""Tests for the curate panel's brand-logo preview (panel.build_view).

The brand logo is never part of the catalog (it's injected only at publish
time by build_collection.py), but the panel should still show a preview card
for it -- without letting it affect the real included/excluded counts or be
sent to /api/save. These tests lock down that separation.
"""

from __future__ import annotations

import json
import random
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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
import panel as p_mod  # noqa: E402
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
                view, by_key, _hidden = p.build_view(cat, bot_username)
                return view, by_key

    def test_logo_shown_first_for_emoji_mapper_bot(self):
        logo = self.data / "logo.png"
        _make_png(logo)
        view, by_key = self._view("YourEmojiBot", logo)
        self.assertTrue(view[0]["isLogo"])
        self.assertEqual(view[0]["key"], p.LOGO_KEY)
        self.assertEqual(by_key[p.LOGO_KEY], logo)
        # The 3 real catalog items still follow, none marked as logo.
        self.assertEqual(len(view), 4)
        self.assertTrue(all(not v.get("isLogo") for v in view[1:]))

    def test_logo_hidden_for_coin_bot(self):
        logo = self.data / "logo.png"
        _make_png(logo)
        view, _ = self._view("YourCoinEmojiBot", logo)
        self.assertEqual(len(view), 3)  # no logo card injected
        self.assertTrue(all(not v.get("isLogo") for v in view))

    def test_logo_hidden_when_file_missing(self):
        missing = self.data / "does_not_exist.png"
        view, _ = self._view("YourEmojiBot", missing)
        self.assertEqual(len(view), 3)

    def test_logo_hidden_when_bot_unknown(self):
        logo = self.data / "logo.png"
        _make_png(logo)
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
            self.view, by_key, _hidden = p.build_view(cat, "")

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
            self.view, by_key, _hidden = p.build_view(cat, "")

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


class CopyTheEmojiId(unittest.TestCase):
    """Clicking the id under a card copies it, and does not toggle the card."""

    def test_only_a_whole_premium_id_label_is_copyable(self):
        self.assertEqual(p.copy_id_for("premium-id:5406926593698312391"),
                         "5406926593698312391")
        # Anchored: a label that merely contains the prefix or trails junk is
        # not an id, and offering it would put the wrong thing on the clipboard.
        for junk in ("xpremium-id:12", "premium-id:12x", "premium-id:",
                     "premium-id:12 34", "", "coin logo", "premium-id:abc"):
            self.assertEqual(p.copy_id_for(junk), "", junk)

    def test_the_view_carries_the_id_for_the_page(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        img = data / "media" / "static" / "a.png"
        _make_png(img)
        other = data / "media" / "static" / "b.png"
        _make_png(other)
        with Catalog(data / "catalog.db") as cat:
            cat.add(content_key="s:" + "a" * 30, fmt="static", file_path=img,
                    emojis=["😀"], keywords=["premium-id:5406926593698312391"])
            cat.add(content_key="s:" + "b" * 30, fmt="static", file_path=other,
                    emojis=["😀"], keywords=["hand drawn"])
        with Catalog(data / "catalog.db") as cat:
            view, _by_key, _hidden = p.build_view(cat, "")
        by_label = {v["label"]: v["copyId"] for v in view}
        self.assertEqual(by_label["premium-id:5406926593698312391"],
                         "5406926593698312391")
        self.assertEqual(by_label["hand drawn"], "")

    def test_the_page_stops_the_click_before_the_toggle(self):
        """The label sits inside the card, so without this a copy also toggles.

        Asserted against the served page because the ordering lives in the
        click handler, not in any Python function.
        """
        page = p.PAGE
        copy_at = page.index("closest('.copyable')")
        toggle_at = page.index("closest('.card')", copy_at - 400)
        self.assertLess(copy_at, toggle_at,
                        "the copy branch must run before the toggle branch")
        self.assertIn("e.stopPropagation()", page[copy_at:copy_at + 200])

    def test_video_plays_without_hover(self):
        """Hover-only playback was rejected: a grid of stills cannot be curated.

        Video is bounded the same way the animated cards are -- by the viewport
        observer and the Animation switch -- not by the mouse.
        """
        page = p.PAGE
        self.assertIn("data-play", page.replace("dataset.play", "data-play"))
        self.assertIn("video[data-play]", page)
        # The remaining hover handlers exist only for prefers-reduced-motion.
        self.assertIn("if (RM) {", page)


class DragAndDropOrdering(unittest.TestCase):
    """Two reported bugs, pinned at the only level available: the served page.

    The reorder itself lives in a drop handler, so there is no Python function
    to call. What these assert is exactly what regressed.
    """

    def test_a_drop_outside_a_card_never_reorders(self):
        """It used to fall back to the LAST position.

        `to = card ? indexOf(card) : ITEMS.length-1` meant releasing over a grid
        gap -- and the gaps between cards are a large target -- silently threw
        the emoji to the end of the pack.
        """
        page = p.PAGE
        self.assertNotIn("card ? ITEMS.findIndex(x=>x.key===card.dataset.key) "
                         ": ITEMS.length-1", page)
        drop = page[page.index("addEventListener('drop'"):]
        guard = drop.index("if(!card){ endDrag(); return; }")
        seek = drop.index("ITEMS.findIndex(x=>x.key===card.dataset.key)")
        self.assertLess(guard, seek,
                        "the no-card guard must run before any index is chosen")

    def test_dragging_to_an_edge_scrolls_the_page(self):
        """Without this the drag is trapped in the current viewport.

        With 200 cards there is otherwise no way to carry #200 up to #10.
        """
        page = p.PAGE
        self.assertIn("function edgeScroll(", page)
        self.assertIn("requestAnimationFrame", page)
        # On the document: at the top of the window the pointer sits over the
        # sticky header, where a grid-only listener never fires.
        doc_over = page.index("document.addEventListener('dragover'")
        self.assertIn("edgeScroll(e.clientY)", page[doc_over:doc_over + 300])

    def test_the_position_number_is_recomputed_not_stored(self):
        """A number written at build time is right once and wrong after a drag.

        renumber() walks ITEMS, which IS the order, so there is no second copy
        to drift. It has to run both after the first render and after a drop.
        """
        page = p.PAGE
        self.assertIn("function renumber(", page)
        self.assertNotIn("el('span','pos', n", page)   # never filled at build time
        render = page[page.index("function render(){"):]
        self.assertIn("renumber();", render[:render.index("function updateCount")])
        # Up to saveOrder(), not to the first endDrag() -- that one is the
        # no-card early return, which sits BEFORE any reordering happens.
        drop = page[page.index("addEventListener('drop'"):]
        self.assertIn("renumber();", drop[:drop.index("saveOrder();")])

    def test_the_logo_IS_numbered_because_it_takes_a_real_slot(self):
        """It leads every set it is added to, so it costs one of the 200.

        build_collection reserves it -- `capacity = per_set - 1` -- so leaving
        it out of the panel's numbering made the panel disagree with what ships:
        the owner read "200" and the pack was 201.
        """
        page = p.PAGE
        hdr = page[page.index("const hdr = el('div','hdr');"):]
        block = hdr[:hdr.index("card.appendChild(hdr);")]
        self.assertIn("hdr.appendChild(el('span','pos',''));", block)
        # The tick is the one thing the logo does NOT get: it is not toggleable.
        self.assertIn("if(!it.isLogo) hdr.appendChild(el('span','tick'", block)
        renumber = page[page.index("function renumber(){"):]
        renumber = renumber[:renumber.index(chr(10) + "}")]
        self.assertNotIn("if(it.isLogo) continue", renumber,
                         "skipping the logo is what made the count wrong")

    def test_a_selection_that_cannot_be_one_pack_says_so(self):
        """200 chosen emoji plus the logo is 201, over Telegram's per-set cap.

        Surfaced in the header rather than discovered as a surprise second set.
        """
        page = p.PAGE
        self.assertIn("included > PER_SET", page)
        self.assertIn("Math.ceil(included / PER_SET)", page)
        # The limit comes from build_collection, not a second copy that drifts.
        self.assertIn("PER_SET", p.PAGE)
        self.assertEqual(p.PER_SET, 200)

    def test_the_card_header_cannot_overlap_itself(self):
        """The badge, the number and the tick shared one ~120px strip.

        Absolutely positioned at left / centre / right, "animated" ran straight
        under the number and both were unreadable. They are now a centred
        COLUMN -- number over format -- with only the tick pinned to the corner,
        which is what keeps it from pushing the stack off centre. The badge is
        still the only one allowed to shrink, and "animated" carries a short
        label because it does not fit.
        """
        page = p.PAGE
        hdr = page[page.index(".hdr{"):]
        rule = hdr[:hdr.index("}")]
        self.assertIn("display:flex", rule)
        self.assertIn("flex-direction:column", rule)
        self.assertIn("align-items:center", rule)
        # In the column flow, so they cannot be placed on top of each other.
        for cls in (".badge{", ".pos{"):
            r = page[page.index(cls):]
            self.assertNotIn("position:absolute", r[:r.index("}")], cls)
        # The tick is the one exception, and deliberately so.
        tick = page[page.index(".tick{"):]
        self.assertIn("position:absolute", tick[:tick.index("}")])
        # The full word fits now that the header is a column, so the
        # abbreviation that the single-strip layout forced is gone.
        self.assertNotIn("FMT = {animated: 'anim'", page)

    def test_the_scroll_loop_cannot_outlive_the_drag(self):
        """A drag released outside the window fires no drop.

        An unguarded rAF loop would then scroll the page forever.
        """
        page = p.PAGE
        step = page[page.index("function stepEdge("):]
        self.assertIn("if(dragKey === null || !edgeSpeed) return;",
                      step[:step.index("}")+400])
        self.assertIn("function stopEdgeScroll(", page)
        self.assertIn("cancelAnimationFrame", page)


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

    def test_the_page_polls_and_keeps_the_warning_up(self):
        page = p.PAGE
        self.assertIn("/api/ping", page)
        self.assertIn("setInterval(", page)
        # A banner, not a toast: the toast auto-hides after 2600ms.
        self.assertIn("id=\"alert\"", page)
        self.assertNotIn("setTimeout(()=>a.classList.remove('show')", page)

    def test_unsaved_work_is_remembered_and_retried(self):
        page = p.PAGE
        self.assertIn("pendingOrder", page)
        # The heartbeat flushes it, so recovery needs no action from the owner.
        beat = page[page.index("setInterval(async ()=>{"):]
        self.assertIn("flushOrder(pendingOrder)", beat[:600])

    def test_a_restarted_panel_does_not_strand_the_page(self):
        """The token is per run, so a restart 403s the page's saves.

        Re-reading it from "/" is same-origin -- exactly the boundary the token
        protects -- so this weakens nothing, and it is the difference between
        "restart the panel and lose your afternoon" and "it catches up".
        """
        page = p.PAGE
        api = page[page.index("async function apiPost("):]
        body = api[:api.index("async function flushOrder")]
        self.assertIn("r.status === 403", body)
        self.assertIn("const TOKEN =", body)     # re-parsed from a fresh "/"

    def test_the_browser_asks_before_closing_on_unsaved_work(self):
        page = p.PAGE
        self.assertIn("beforeunload", page)
        guard = page[page.index("addEventListener('beforeunload'"):]
        self.assertIn("if(pendingOrder)", guard[:200])


class UndoRedoAndFormatColours(unittest.TestCase):
    """The header controls, the history stack, and telling formats apart."""

    def test_every_mutation_records_the_state_to_return_to(self):
        """remember() must run BEFORE the change, at all three sites.

        A missed site is invisible until someone undoes past it and gets the
        wrong state back, which is worse than having no undo at all.
        """
        page = p.PAGE
        for label, marker, mutation in (
            ("select all / invert", "function setAll(fn){", "it.included = fn(it)"),
            ("card toggle", "if(i < 0 || ITEMS[i].isLogo) return;", "ITEMS[i].included=!ITEMS[i].included"),
            ("drag reorder", "if(from>=0 && to>=0 && to!==from){", "ITEMS.splice(from,1)"),
        ):
            block = page[page.index(marker):]
            block = block[:block.index(mutation)]
            self.assertIn("remember()", block, f"{label} does not record history first")

    def test_the_history_is_bounded(self):
        """A long curation session must not grow the stack without limit."""
        page = p.PAGE
        self.assertIn("HISTORY_MAX", page)
        block = page[page.index("function remember(){"):]
        self.assertIn("past.shift()", block[:block.index("}")+200])

    def test_a_new_action_drops_the_redo_branch(self):
        page = p.PAGE
        block = page[page.index("function remember(){"):]
        self.assertIn("future.length = 0", block[:block.index("updateHistoryButtons")])

    def test_undo_moves_the_cards_instead_of_rebuilding_them(self):
        """render() here would re-request all 200 thumbnails and previews.

        Appending a node that is already in the document relocates it, so the
        loaded media survives an undo.
        """
        page = p.PAGE
        block = page[page.index("function applySnapshot("):]
        block = block[:block.index("function undo()")]
        # Comments explain what the code deliberately does NOT do, so they
        # mention render() -- strip them or the assertion matches the prose.
        code = chr(10).join(ln for ln in block.splitlines()
                            if not ln.strip().startswith("//"))
        self.assertIn("grid.appendChild(frag)", code)
        self.assertNotIn("render()", code)
        # Order auto-saves, so an undone reorder must reach the catalog too.
        self.assertIn("saveOrder()", code)

    def test_each_format_has_its_own_accent(self):
        page = p.PAGE
        colours = {}
        for fmt in ("static", "animated", "video"):
            rule = page[page.index(f".card.fmt-{fmt}"):]
            colours[fmt] = rule[rule.index("--fmt:") + 6:rule.index(";")]
        self.assertEqual(len(set(colours.values())), 3, colours)
        # The drag-over highlight must not be any format's colour, or a drop
        # target reads as "this card is animated".
        over = page[page.index(".card.over{"):]
        over = over[:over.index("}")]
        for fmt, c in colours.items():
            self.assertNotIn(c, over, f"drop target uses the {fmt} colour")

    def test_two_frames_answer_two_questions(self):
        """Inner frame = the format, outer frame = whether it is selected.

        One frame carrying both is what the owner rejected: a per-format card
        border striped the whole dark grid.

        The format frame is an OUTLINE, not a border. A border is drawn inside
        the box, so it ate two pixels off every thumbnail and sat flush against
        the artwork; an outline is painted outside and resizes nothing.
        """
        page = p.PAGE
        thumb = page[page.index(".thumb{"):]
        rule = thumb[:thumb.index("}")]
        self.assertIn("outline:2px solid var(--fmt", rule)
        self.assertIn("outline-offset:", rule,
                      "without an offset the frame still touches the artwork")
        self.assertNotIn("border:2px solid var(--fmt", rule,
                         "an inner border shrinks the thumbnail it frames")
        card_on = page[page.index(".card.on{"):]
        self.assertIn("#22c55e", card_on[:card_on.index("}")],
                      "the selected frame must be green")

    def test_the_animation_control_is_a_switch(self):
        """On/off state shown by the control itself, not only by its label."""
        page = p.PAGE
        self.assertIn('aria-pressed', page)
        self.assertIn('#anim[aria-pressed="true"]  .knob{background:#22c55e}', page)
        self.assertIn('#anim[aria-pressed="false"] .knob{background:#f43f5e}', page)
        self.assertIn("--btn:#34ebc6", page)

    def test_the_card_header_stacks_number_over_format(self):
        page = p.PAGE
        hdr = page[page.index(".hdr{"):]
        rule = hdr[:hdr.index("}")]
        self.assertIn("flex-direction:column", rule)
        self.assertIn("align-items:center", rule)
        # The number is appended before the format badge, so it sits on top.
        markup = page[page.index("const hdr = el('div','hdr');"):]
        markup = markup[:markup.index("card.appendChild(hdr);")]
        self.assertLess(markup.index("'pos'"), markup.index("'badge'"))

    def test_only_save_selection_sits_outside_the_centre_group(self):
        page = p.PAGE
        actions = page[page.index('<div class="actions">'):]
        actions = actions[:actions.index("</div>")]
        for btn in ("undo", "redo", "all", "none", "inv", "bg", "anim"):
            self.assertIn(f'id="{btn}"', actions, btn)
        self.assertNotIn('id="save"', actions,
                         "Save writes; it stays out of the centre group")
        # Centred by grid columns, not by flex spacers -- spacers only centre
        # when both sides weigh the same, and the title is far wider.
        self.assertIn("grid-template-columns:1fr auto 1fr", page)


class OffScreenCostsNothing(unittest.TestCase):
    """A card you cannot see must not be decoding frames.

    Two hundred cards, 147 of them animated: if leaving the viewport did not
    stop them the grid would decode every one of them forever, which is what
    "the page is heavy" actually meant.
    """

    def test_leaving_the_viewport_stops_video_AND_animation(self):
        io_block = p.PAGE[p.PAGE.index("const animIO"):]
        io_block = io_block[:io_block.index("}, {root:")]
        # One flag decides both media kinds, and it is driven by intersection.
        self.assertIn("e.isIntersecting", io_block)
        self.assertIn("setPlaying(t, live)", io_block,
                      "video must be paused when it scrolls away, not muted")
        self.assertIn("t.dataset.still", io_block,
                      "an animated image must fall back to its single frame")
        # Coming back must restore it -- a one-way stop would leave a dead grid.
        self.assertIn("t.dataset.anim", io_block)

    def test_every_card_is_observed_once_the_grid_is_built(self):
        """render() creates every node, so it is what must start observing.

        Reordering does NOT rebuild: undo/redo and drag move the existing
        nodes, and appending a node already in the document relocates it. That
        is why observation survives a reorder -- and why the check that matters
        is on the one function that makes the nodes in the first place.
        """
        body = p.PAGE[p.PAGE.index("function render("):]
        body = body[:body.index("function updateCount(")]
        self.assertIn("observeAnimated()", body,
                      "a freshly built grid nobody observes never pauses")

    def test_the_sticky_header_does_not_blur_its_backdrop(self):
        """backdrop-filter re-blurs everything behind it on every scroll frame.

        It is the most expensive thing a sticky bar can do, and over an opaque
        background it buys nothing.
        """
        header = p.PAGE[p.PAGE.index("header{"):]
        # Strip CSS comments first. Twice now a check like this has passed or
        # failed on the COMMENT explaining the rule rather than the rule.
        rule = re.sub(r"/\*.*?\*/", "", header[:header.index("}")], flags=re.S)
        self.assertNotIn("backdrop-filter", rule)


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


class PublishedEmojiLeaveTheGrid(unittest.TestCase):
    """The panel arranges the pack being BUILT: anything live is out.

    The rule was "hide only a FULL set" for one round. `--new-set` broke that
    theory -- a pack can be left half-empty deliberately, so a set that is not
    full is not therefore unfinished, and the owner kept being shown an
    abandoned pack's emoji while curating the next one. Published is the
    property that decides it, and it needs no publish_*.json and no capacity
    arithmetic.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(5):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            for i in (0, 1):                      # in the FULL set
                cat.mark_uploaded(f"s:item{i:030d}", f"cid{i}",
                                  base="pk", set_name="pk1_by_bot")
            cat.mark_uploaded(f"s:item{2:030d}", "cid2",   # in the HALF-EMPTY set
                              base="pk", set_name="pk2_by_bot")

    def tearDown(self):
        self.tmp.cleanup()

    def _view(self, show_published=False):
        with Catalog(self.db) as cat:
            return p.build_view(cat, "", show_published)

    def test_a_half_empty_pack_is_hidden_too_once_it_is_published(self):
        """The regression the owner reported twice: pack 2 kept coming back."""
        view, _by_key, hidden = self._view()
        keys = [v["key"] for v in view]
        self.assertEqual(hidden, 3, "two in the full set AND the half-empty one")
        self.assertNotIn(f"s:item{2:030d}", keys,
                         "published into a 3/200 set is still published")
        for i in (3, 4):
            self.assertIn(f"s:item{i:030d}", keys, "never published at all")

    def test_no_publish_state_file_is_needed(self):
        """The old rule read publish_*.json; this one asks the catalog."""
        self.assertEqual(list(self.data.glob("publish_*.json")), [])
        _view, _bk, hidden = self._view()
        self.assertEqual(hidden, 3)

    def test_a_row_with_no_recorded_set_name_still_counts_as_published(self):
        """Unknown WHERE is not unknown WHETHER -- it must not be offered up."""
        with Catalog(self.db) as cat:
            cat.mark_uploaded(f"s:item{3:030d}", "cid3", base="pk",
                              set_name=None)
        view, _bk, _h = self._view()
        self.assertNotIn(f"s:item{3:030d}", [v["key"] for v in view])

    def test_all_shows_everything(self):
        view, _bk, hidden = self._view(show_published=True)
        self.assertEqual(hidden, 0)
        self.assertEqual(len(view), 5)

    def test_hiding_deletes_nothing(self):
        self._view()
        with Catalog(self.db) as cat:
            self.assertEqual(len(cat.all_items()), 5, "no row was removed")
            self.assertTrue(cat.is_published("pk", f"s:item{0:030d}"))


class SavingFromAFilteredGridKeepsHiddenChoices(unittest.TestCase):
    """A view that hides things cannot speak for what it hides.

    `set_inclusion` re-includes every key it is NOT given, so a save posted
    from a grid that hides finished packs would silently re-include every
    hidden item that had been deselected. The panel already carries a comment
    about an earlier variant of exactly this; the filter reintroduced it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(4):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            for i in (0, 1):
                cat.mark_uploaded(f"s:item{i:030d}", f"cid{i}",
                                  base="pk", set_name="pk1_by_bot")
            # the owner deselected one of the FINISHED pack's emoji
            cat.set_inclusion({f"s:item{0:030d}"})
        (self.data / "publish_pk.json").write_text(json.dumps({
            "sets": [{"name": "pk1_by_bot", "index": 1, "live": p.PER_SET,
                      "logo": True}]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_hidden_exclusion_survives_a_save_from_the_filtered_grid(self):
        with Catalog(self.db) as cat:
            view, _bk, hidden = p.build_view(cat, "", False)
        self.assertEqual(hidden, 2, "the published pack is out of the grid")
        visible = {v["key"] for v in view if not v.get("isLogo")}
        self.assertNotIn(f"s:item{0:030d}", visible)

        # What the handler does: intersect the request with what is shown, then
        # carry every hidden exclusion through.
        raw: set[str] = set()                       # nothing deselected on screen
        with Catalog(self.db) as cat:
            hidden_excluded = {it.content_key for it in cat.all_items()
                               if not it.included and it.content_key not in visible}
            cat.set_inclusion((raw & visible) | hidden_excluded)
            still = {it.content_key for it in cat.all_items() if not it.included}
        self.assertEqual(still, {f"s:item{0:030d}"},
                         "the hidden de-selection must not be undone")


class TheBusyPortMessageNamesTheProcess(unittest.TestCase):
    """"Press Ctrl+C in the window running it" is useless with no window.

    Every stray panel so far was started detached, so the advice pointed at a
    window that does not exist. The pid does.
    """

    SAMPLE = chr(10).join([
        "Active Connections",
        "",
        "  Proto  Local Address      Foreign Address    State       PID",
        "  TCP    127.0.0.1:9450     0.0.0.0:0          LISTENING   4321",
        "  TCP    127.0.0.1:9451     0.0.0.0:0          LISTENING   9999",
        "  TCP    10.0.0.5:59450     1.2.3.4:443        ESTABLISHED 1111",
    ])

    def _holder(self, port, stdout=None, boom=None):
        def run(*a, **kw):
            if boom:
                raise boom
            text = self.SAMPLE if stdout is None else stdout
            return type("R", (), {"stdout": text})()

        with mock.patch.object(p.subprocess, "run", run):
            return p._port_holder(port)

    def test_it_finds_the_listener(self):
        self.assertEqual(self._holder(9450), 4321)
        self.assertEqual(self._holder(9451), 9999)

    def test_an_established_connection_is_not_a_listener(self):
        self.assertIsNone(self._holder(443))

    def test_a_port_nobody_listens_on_is_none(self):
        self.assertIsNone(self._holder(1234))

    def test_it_never_raises(self):
        """It runs only to improve an error message."""
        self.assertIsNone(self._holder(9450, boom=OSError("no netstat")))
        self.assertIsNone(self._holder(9450, stdout="garbage" + chr(10)))


class TheSandboxCannotTakeTheRealPanelsPort(unittest.TestCase):
    """One number, one place.

    The sandbox exists to stay off the port a real panel serves on. Both sides
    used to spell the number out, so moving the panel would have silently
    pointed the sandbox at it -- and a synthetic drag against the REAL catalog
    is how three hours of manual ordering were destroyed once.
    """

    def _sandbox(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import panel_sandbox
        return panel_sandbox

    def test_the_sandbox_derives_its_port_from_the_panel(self):
        ps = self._sandbox()
        self.assertEqual(ps.PANEL_PORT, p.DEFAULT_PORT,
                         "the sandbox must read the panel's port, not repeat it")
        self.assertNotEqual(ps.DEFAULT_PORT, p.DEFAULT_PORT)

    def test_it_refuses_to_be_told_to_use_it(self):
        ps = self._sandbox()
        with self.assertRaises(SystemExit) as caught:
            ps.main(["--port", str(p.DEFAULT_PORT)])
        self.assertIn(str(p.DEFAULT_PORT), str(caught.exception))


class OnlyOnePanelPerPort(unittest.TestCase):
    """A second panel must REFUSE the port, not quietly bind over the first.

    socketserver sets SO_REUSEADDR by default and on Windows that does not mean
    what it means on Linux: the second bind SUCCEEDS. Two panels then run, both
    logging "Panel at ...", the browser reaches whichever socket the OS picks,
    and the older process keeps serving its own start-up snapshot -- which is
    why refreshing appeared to do nothing and only closing the launcher, which
    kills every instance, made a change appear.
    """

    TIMEOUT = 60

    def test_the_second_instance_exits_instead_of_sharing_the_port(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        img = data / "media" / "static" / "a.png"
        _make_png(img)
        with Catalog(data / "catalog.db") as cat:
            cat.add(content_key="s:" + "a" * 30, fmt="static", file_path=img,
                    emojis=["😀"], keywords=["one"])

        with socket.socket() as probe:          # a free port, chosen by the OS
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        argv = [sys.executable, str(ROOT / "panel.py"), "--data-dir", str(data),
                "--port", str(port), "--no-open"]
        first = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)

        def stop_first():
            # kill() alone leaves a zombie and an open pipe -- the guarded
            # runner reports both, and a leaked process holding the port would
            # make the NEXT run of this test fail for the wrong reason.
            if first.poll() is None:
                first.kill()
            try:
                first.communicate(timeout=self.TIMEOUT)
            except subprocess.TimeoutExpired:
                first.kill()
                first.communicate(timeout=self.TIMEOUT)

        self.addCleanup(stop_first)

        # Bounded polling on a real readiness signal, never a blind sleep.
        deadline = time.monotonic() + self.TIMEOUT
        ready = False
        while time.monotonic() < deadline:
            if first.poll() is not None:
                out = first.communicate(timeout=self.TIMEOUT)[0] or ""
                self.fail(f"the first panel exited early: {out[-400:]}")
            try:
                with request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                    ready = r.status == 200
                    break
            except (error.URLError, OSError):
                continue
        self.assertTrue(ready, "the first panel never became reachable")

        second = subprocess.run(argv, capture_output=True, text=True,
                                timeout=self.TIMEOUT)
        self.assertEqual(second.returncode, 2, second.stdout[-400:])
        self.assertIn("already running", second.stdout + second.stderr)


class OnePackCanBeUnhidden(unittest.TestCase):
    """`--with-pack N` re-opens ONE published set, not all of them.

    `--all` is the wrong tool for arranging a half-full pack: it also brings
    back every finished pack, which on the real catalog is hundreds of cards
    nothing can be done with.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db) as cat:
            for i in range(6):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            for i in (0, 1):
                cat.mark_uploaded(f"s:item{i:030d}", f"cid{i}",
                                  base="pk", set_name="pk1_by_bot")
            for i in (2, 3):
                cat.mark_uploaded(f"s:item{i:030d}", f"cid{i}",
                                  base="pk", set_name="pk2_by_bot")
            # 4 and 5 stay unpublished: the new candidates.
        (self.data / "publish_pk.json").write_text(json.dumps({
            "sets": [{"name": "pk1_by_bot", "index": 1, "live": 3, "logo": True},
                     {"name": "pk2_by_bot", "index": 2, "live": 3, "logo": True}],
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _keys(self, keep=None):
        with Catalog(self.db) as cat:
            view, _bk, hidden = p.build_view(cat, "", False, keep)
        return {v["key"] for v in view if not v.get("isLogo")}, hidden

    def test_an_index_resolves_through_the_publishers_own_state(self):
        """Not by rebuilding '<base><n>_by_<bot>' -- the file records it."""
        self.assertEqual(p.packs_named(self.data, {2}), {"pk2_by_bot"})
        self.assertEqual(p.packs_named(self.data, {1, 2}),
                         {"pk1_by_bot", "pk2_by_bot"})
        self.assertEqual(p.packs_named(self.data, {9}), set(), "no such pack")

    def test_only_the_named_pack_comes_back(self):
        keys, hidden = self._keys(p.packs_named(self.data, {2}))
        self.assertEqual(hidden, 2, "pack 1 stays hidden")
        for i in (2, 3):
            self.assertIn(f"s:item{i:030d}", keys, "pack 2 is visible")
        for i in (0, 1):
            self.assertNotIn(f"s:item{i:030d}", keys, "pack 1 is not")
        for i in (4, 5):
            self.assertIn(f"s:item{i:030d}", keys, "candidates always show")

    def test_without_the_flag_every_published_pack_stays_hidden(self):
        keys, hidden = self._keys(None)
        self.assertEqual(hidden, 4)
        self.assertEqual(keys, {f"s:item{i:030d}" for i in (4, 5)})

    def test_a_row_with_no_recorded_set_is_not_unhidden_by_guesswork(self):
        """Unknown WHERE must not be answered with "probably that one"."""
        with Catalog(self.db) as cat:
            cat.mark_uploaded(f"s:item{4:030d}", "cid4", base="pk", set_name=None)
        keys, _h = self._keys(p.packs_named(self.data, {2}))
        self.assertNotIn(f"s:item{4:030d}", keys)


class PackSplitsAndJumpButtons(unittest.TestCase):
    """The pack-boundary markers, and the two jump buttons beside them."""

    def test_a_separator_is_never_a_card(self):
        """The drop handler resolves its target with closest('.card').

        A separator carrying that class would sit between cards, swallow a drop
        aimed past it and do nothing -- the same shape as the bug where a drop
        on a grid gap silently threw the emoji to the end. It is `.packsep`.
        """
        page = p.PAGE
        css = page[page.index(".packsep{"):]
        self.assertIn("grid-column:1/-1", css[:css.index("}")])
        body = page[page.index("function makeSep("):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("el('div','packsep')", body)
        self.assertNotIn("'card'", body)
        self.assertNotIn("packsep card", page)
        # And the drop handler still keys off .card, so the two cannot meet.
        self.assertIn("e.target.closest('.card')", page)

    def test_the_splits_are_counted_from_included_items_only(self):
        """An unticked card never ships, so it cannot push the boundary."""
        body = p.PAGE[p.PAGE.index("function renumber("):]
        body = body[:body.index("\n// Only the cards you can actually see")] \
            if "\n// Only the cards you can actually see" in body else body[:4000]
        self.assertIn("!it.isLogo && it.included", body)
        # capacity leaves a slot for the logo, exactly as build_collection does.
        self.assertIn("PER_SET - (logo ? 1 : 0)", body)

    def test_selection_changes_recompute_the_splits(self):
        """Both inclusion paths must renumber, not just update the counter.

        Only reorder called renumber() before; unticking enough cards genuinely
        moves a boundary, so a counter-only refresh left the markers lying.
        """
        page = p.PAGE
        self.assertIn("renumber(); updateCount(); }", page)     # setAll
        self.assertIn("lastIdx=i; renumber(); updateCount();", page)  # one card

    def test_separators_are_rebuilt_rather_than_accumulated(self):
        body = p.PAGE[p.PAGE.index("function renumber("):]
        self.assertIn("querySelectorAll('.packsep')", body[:600])
        self.assertIn("s.remove()", body[:600])

    def test_one_pack_needs_no_divider(self):
        body = p.PAGE[p.PAGE.index("function renumber("):]
        self.assertIn("if(starts.length < 2) return;", body)

    def test_the_header_offers_top_and_bottom(self):
        page = p.PAGE
        self.assertIn('id="top"', page)
        self.assertIn('id="bot"', page)
        # Document scrolling, NOT scrollIntoView: that aligns with the top of
        # the viewport, which sits behind the sticky header, so Top stopped one
        # header short of the Pack 1 marker and Bottom stopped short too.
        jump = page[page.index("document.getElementById('top').onclick"):]
        jump = jump[:400]
        self.assertIn("window.scrollTo({top:0})", jump)
        self.assertIn("document.documentElement.scrollHeight", jump)
        self.assertNotIn("scrollIntoView", jump)


if __name__ == "__main__":
    unittest.main()
