"""Tests for what the curate panel builds and serves from the catalog.

The brand logo is never part of the catalog (it is injected only at publish
time by build_collection.py), but the panel should still show a preview card
for it -- without letting it affect the real included/excluded counts or be
sent to /api/save. These tests lock down that separation, together with the
view filtering, the similarity order and the concurrent-save window.

The page-text assertions live in test_panel_page.py, the POST guard in
test_panel_guard.py and the process/port behaviour in test_panel_server.py.
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

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

import panel as p
from emojikit.catalog import Catalog
from emojikit.identity import hamming


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
        view, by_key = self._view("GodVerifyEmojiMapperbot", logo)
        self.assertTrue(view[0]["isLogo"])
        self.assertEqual(view[0]["key"], p.LOGO_KEY)
        self.assertEqual(by_key[p.LOGO_KEY], logo)
        # The 3 real catalog items still follow, none marked as logo.
        self.assertEqual(len(view), 4)
        self.assertTrue(all(not v.get("isLogo") for v in view[1:]))

    def test_logo_hidden_for_coin_bot(self):
        logo = self.data / "logo.png"
        _make_png(logo)
        view, _ = self._view("GodVerifyCoinEmojiMapperbot", logo)
        self.assertEqual(len(view), 3)  # no logo card injected
        self.assertTrue(all(not v.get("isLogo") for v in view))

    def test_logo_hidden_when_file_missing(self):
        missing = self.data / "does_not_exist.png"
        view, _ = self._view("GodVerifyEmojiMapperbot", missing)
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
    """The original greedy walk, written against ``identity.hamming``."""
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
    identity.hamming's exact result, first-minimum tie-break included.
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


if __name__ == "__main__":
    unittest.main()
