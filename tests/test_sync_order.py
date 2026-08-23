"""Reordering a PUBLISHED pack to match the curate panel.

setStickerPositionInSet moves an existing sticker, so nothing is re-uploaded
and every custom_emoji_id survives -- which is the only reason this is safe to
run on a pack people have already installed.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


if __name__ == "__main__":
    unittest.main()
