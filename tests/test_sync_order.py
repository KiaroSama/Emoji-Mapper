"""Reordering a PUBLISHED pack to match the curate panel.

setStickerPositionInSet moves an existing sticker, so nothing is re-uploaded
and every custom_emoji_id survives -- which is the only reason this is safe to
run on a pack people have already installed.
"""

from __future__ import annotations

import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
