"""Reordering a PUBLISHED pack to match the curate panel.

setStickerPositionInSet moves an existing sticker, so nothing is re-uploaded
and every custom_emoji_id survives -- which is the only reason this is safe to
run on a pack people have already installed.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collection_state as cs  # noqa: E402
import sync_order as so  # noqa: E402


def _live(*cids: str) -> list[dict]:
    return [{"custom_emoji_id": c, "file_id": f"f-{c}"} for c in cids]


class PlanMoves(unittest.TestCase):
    def test_an_already_correct_set_needs_no_calls(self):
        live = _live("logo", "a", "b", "c")
        self.assertEqual(so.plan_moves(live, ["a", "b", "c"], 1), [])

    def test_it_sorts_into_the_wanted_order(self):
        live = _live("logo", "c", "a", "b")
        moves = so.plan_moves(live, ["a", "b", "c"], 1)
        # Replay the plan and check the result, rather than asserting a
        # particular move list -- any correct plan is acceptable.
        order = [st["custom_emoji_id"] for st in live]
        for target, cid, _fid in moves:
            order.insert(target, order.pop(order.index(cid)))
        self.assertEqual(order, ["logo", "a", "b", "c"])

    def test_the_pinned_logo_is_never_moved(self):
        live = _live("logo", "c", "b", "a")
        moves = so.plan_moves(live, ["a", "b", "c"], 1)
        self.assertTrue(all(target >= 1 for target, _c, _f in moves), moves)
        self.assertNotIn("logo", [cid for _t, cid, _f in moves])

    def test_a_full_reversal_still_lands_exactly(self):
        n = 30
        cids = [f"e{i}" for i in range(n)]
        live = _live("logo", *reversed(cids))
        moves = so.plan_moves(live, cids, 1)
        order = [st["custom_emoji_id"] for st in live]
        for target, cid, _fid in moves:
            order.insert(target, order.pop(order.index(cid)))
        self.assertEqual(order, ["logo"] + cids)
        # Selection sort: never more moves than there are stickers to place.
        self.assertLessEqual(len(moves), n)


class ReorderIsIdempotent(unittest.TestCase):
    """Unlike addStickerToSet, this one may simply be retried."""

    def test_replaying_a_plan_twice_changes_nothing_the_second_time(self):
        live = _live("logo", "b", "c", "a")
        moves = so.plan_moves(live, ["a", "b", "c"], 1)
        order = [st["custom_emoji_id"] for st in live]
        for target, cid, _f in moves:
            order.insert(target, order.pop(order.index(cid)))
        again = so.plan_moves(_live(*order), ["a", "b", "c"], 1)
        self.assertEqual(again, [], "a synced set must need no further moves")


class TheRecordedOrderFollowsTheLiveOne(unittest.TestCase):
    """Reordering a set must rewrite what the publisher recorded about it.

    The publisher checks every recorded position by identity before adding to a
    set, so a reorder that leaves the record behind makes the whole family
    unpublishable. It did: the next publish stopped on "position 117 now holds a
    sticker this publisher cannot identify".
    """

    class _Cat:
        def __init__(self, pairs):
            self._items = [type("It", (), {"custom_emoji_id": c,
                                           "content_key": k})()
                           for c, k in pairs]

        def all_items(self):
            return list(self._items)

    def _tg(self):
        tg = mock.Mock()
        tg.set_sticker_position = mock.Mock()
        return tg

    def _set(self, live_cids, name="pk1_by_bot"):
        return {"name": name, "logo": True,
                "stickers": _live("logo", *live_cids)}

    def test_a_reorder_rewrites_the_recorded_keys(self):
        cat = self._Cat([("a", "s:a"), ("b", "s:b"), ("c", "s:c")])
        so.desired_order = lambda _c, live: [c for c in ("a", "b", "c") if c in live]
        tg = self._tg()
        tg.get_sticker_set.return_value = {"stickers": _live("logo", "c", "a", "b")}
        rec = {"name": "pk1_by_bot", "logo": True, "keys": ["s:c", "s:a", "s:b"]}
        so.sync_set(tg, cat, rec, apply=True)
        self.assertEqual(rec["keys"], ["s:a", "s:b", "s:c"],
                         "the record must describe the order the set is in now")

    def test_a_stale_record_is_repaired_even_with_nothing_to_move(self):
        """The set can already be right while the RECORD is wrong.

        That is exactly what an earlier reorder left behind, so the repair
        cannot be gated on there being moves to make.
        """
        cat = self._Cat([("a", "s:a"), ("b", "s:b")])
        so.desired_order = lambda _c, live: [c for c in ("a", "b") if c in live]
        tg = self._tg()
        tg.get_sticker_set.return_value = {"stickers": _live("logo", "a", "b")}
        rec = {"name": "pk1_by_bot", "logo": True, "keys": ["s:b", "s:a"]}
        self.assertEqual(so.sync_set(tg, cat, rec, apply=True), 0)
        tg.set_sticker_position.assert_not_called()
        self.assertEqual(rec["keys"], ["s:a", "s:b"])

    def test_a_report_only_run_leaves_the_record_alone(self):
        cat = self._Cat([("a", "s:a"), ("b", "s:b")])
        so.desired_order = lambda _c, live: [c for c in ("a", "b") if c in live]
        tg = self._tg()
        tg.get_sticker_set.return_value = {"stickers": _live("logo", "b", "a")}
        rec = {"name": "pk1_by_bot", "logo": True, "keys": ["s:b", "s:a"]}
        so.sync_set(tg, cat, rec, apply=False)
        self.assertEqual(rec["keys"], ["s:b", "s:a"], "a dry run must write nothing")


class OnePackAtATime(unittest.TestCase):
    """A run that arranged ONE pack must not move stickers in the others.

    Publishing appends; it cannot move a sticker that is already live. So after
    arranging pack 2 in the panel and publishing into it, the live order still
    has to be applied separately -- and applying it to the WHOLE family would
    also reorder packs the owner never looked at. Pack 5 was 28 moves away and
    pack 1 one move away at the time this was added.
    """

    INDEXES = (1, 2, 5)

    def setUp(self):
        # main() refuses before the loop when the catalog file is missing, so a
        # made-up data dir makes every one of these pass for the wrong reason --
        # the unknown-pack test did exactly that until this was added.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / "catalog.db").write_bytes(b"")
        # A REAL state file, not a patched load_state: main() checks the file
        # exists before it ever calls the loader, so patching the loader alone
        # left every case exiting early -- passing for the wrong reason.
        cs.save_json(cs._state_path(self.data, "pk"), {
            "base": "pk", "sent": [], "skipped": [],
            "sets": [{"index": i, "name": f"pks{i}_by_bot", "fmt": "static",
                      "title": f"Pack {i}", "live": 2, "logo": True,
                      "keys": [f"s:{i}"]} for i in self.INDEXES]})

    @property
    def argv(self) -> list[str]:
        return ["--base", "pk", "--data-dir", str(self.data)]

    def _run(self, argv, synced):
        def fake_sync(tg, cat, rec, *, apply):
            synced.append(rec["index"])
            return 0

        patches = [
            mock.patch.object(so, "sync_set", fake_sync),
            mock.patch.object(so, "Telegram", lambda t: mock.Mock()),
            mock.patch.object(so, "Catalog", lambda p: mock.Mock()),
            mock.patch.object(so, "exclusive_lock",
                              lambda p: contextlib.nullcontext()),
            mock.patch.dict(so.os.environ, {"GENERAL_BOT_TOKEN": "x"}),
            mock.patch.object(so, "setup_logging", lambda *a, **k: None),
            mock.patch.object(so, "load_env", lambda: None),
            redirect_stdout(io.StringIO()),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return so.main(argv)

    def test_without_the_flag_every_pack_is_visited(self):
        seen = []
        self._run(self.argv, seen)
        self.assertEqual(seen, [1, 2, 5])

    def test_pack_limits_the_run_to_that_set(self):
        seen = []
        self._run(self.argv + ["--pack", "2"], seen)
        self.assertEqual(seen, [2], "the other packs must not be touched")

    def test_pack_is_repeatable(self):
        seen = []
        self._run(self.argv + ["--pack", "2", "--pack", "5"], seen)
        self.assertEqual(seen, [2, 5])

    def test_an_unknown_pack_is_a_usage_error_not_a_silent_no_op(self):
        """Reporting success having reordered nothing is the worse failure."""
        seen = []
        rc = self._run(self.argv + ["--pack", "9"], seen)
        self.assertEqual(rc, so.EXIT_USAGE)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
