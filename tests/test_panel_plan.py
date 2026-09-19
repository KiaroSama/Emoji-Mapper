"""The move plan the panel writes for whoever rearranges the real packs."""

import json
import tempfile
import unittest
from pathlib import Path

from emojikit.panel_plan import PLAN_NAME, build_plan, merge_plan, write_plan


def card(key, *, pack=None, included=True, label=None, logo=False):
    out = {"key": key, "included": included, "label": label or key}
    if pack is not None:
        out["pack"] = pack
    if logo:
        out["isLogo"] = True
    return out


class ThePlanSaysWhatTheOwnerDecided(unittest.TestCase):

    def test_an_emoji_dragged_into_another_pack_is_recorded_as_a_move(self):
        view = [card("a", pack=2), card("b", pack=3)]
        plan = build_plan(view, {"a": 3, "b": 3}, 200)
        self.assertEqual(plan["moves"],
                         [{"key": "a", "label": "a", "from_pack": 2, "to_pack": 3}])

    def test_an_emoji_the_panel_has_no_opinion_about_is_absent(self):
        """Saying nothing and saying "leave it" are different claims, and only
        the first is true of an emoji the page never carried a pack for."""
        view = [card("a", pack=2), card("b", pack=2)]
        plan = build_plan(view, {"a": 2}, 200)
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["counts"], {"2": 1})   # only `a` was accounted for

    def test_a_parked_emoji_is_held_not_moved(self):
        """Parking one is how room is made for an arriving emoji, so the step
        that does the real work needs it named -- but it is not a move: the
        owner has not said where it goes."""
        view = [card("a", pack=3, included=False), card("b", pack=3)]
        plan = build_plan(view, {"a": 3, "b": 3}, 200)
        self.assertEqual(plan["held"],
                         [{"key": "a", "label": "a", "from_pack": 3}])
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["counts"], {"3": 1}, "a held emoji fills no slot")

    def test_the_brand_logo_is_not_part_of_the_plan(self):
        view = [card("__logo_pack_1__", pack=1, logo=True), card("a", pack=1)]
        plan = build_plan(view, {"__logo_pack_1__": 2, "a": 1}, 200)
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["counts"], {"1": 1})

    def test_a_candidate_that_was_never_live_is_counted_but_never_a_move(self):
        """It has no `from`, so there is nothing to move it out of -- it is
        simply published into the pack the layout puts it in."""
        view = [card("new", included=True)]
        plan = build_plan(view, {"new": 4}, 200)
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["counts"], {"4": 1})

    def test_an_over_full_pack_is_reported_rather_than_silently_fine(self):
        """The panel refuses an over-full drop while curating, but a plan read
        back later must still be able to say so instead of looking healthy and
        failing at publish time."""
        view = [card(str(i), pack=1) for i in range(3)]
        plan = build_plan(view, {str(i): 1 for i in range(3)}, per_set=2)
        self.assertEqual(plan["over_capacity"], {"1": 3})
        self.assertEqual(plan["counts"], {"1": 3})

    def test_it_round_trips_through_the_file_it_writes(self):
        plan = build_plan([card("a", pack=2)], {"a": 5}, 200)
        with tempfile.TemporaryDirectory() as folder:
            path = write_plan(Path(folder), plan)
            self.assertEqual(path.name, PLAN_NAME)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), plan)



class TheMergeDropsDecisionsAboutEmojiThatAreGone(unittest.TestCase):
    """`live` is the catalog's own key set, read under the same lease.

    Without it the merge can only add: a page sees part of the catalog, so every
    out-of-scope decision is preserved unconditionally and a key deleted since is
    kept forever -- counted, and able to report an over_capacity for a pack
    nobody can find.
    """

    def merged(self, previous, view, targets, scope, **kw):
        return merge_plan(previous, view, targets, set(scope), 200, **kw)

    def test_a_target_for_a_deleted_emoji_is_dropped(self):
        previous = {"version": 1, "targets": [["gone", 4], ["stays", 5]],
                    "known": ["gone", "stays"], "excluded": [],
                    "moves": [], "held": []}
        plan = self.merged(previous, [card("here", pack=1)], {"here": 1}, {"here"},
                           live={"here", "stays"})
        self.assertEqual(dict(plan["targets"]), {"here": 1, "stays": 5})
        self.assertNotIn("gone", plan["known"])
        self.assertNotIn("4", plan["counts"], "a deleted emoji fills no slot")

    def test_without_the_catalog_it_still_only_adds(self):
        """The old contract, so a caller that cannot supply the set stays safe."""
        previous = {"version": 1, "targets": [["gone", 4]], "known": ["gone"],
                    "excluded": [], "moves": [], "held": []}
        plan = self.merged(previous, [card("here", pack=1)], {"here": 1}, {"here"})
        self.assertEqual(dict(plan["targets"]), {"gone": 4, "here": 1})

    def test_a_held_row_for_a_deleted_emoji_is_dropped(self):
        previous = {"version": 1, "targets": [], "known": ["gone"],
                    "excluded": ["gone"], "moves": [],
                    "held": [{"key": "gone", "label": "gone", "from_pack": 3}]}
        plan = self.merged(previous, [card("here", pack=1)], {"here": 1}, {"here"},
                           live={"here"})
        self.assertEqual(plan["held"], [])
        self.assertEqual(plan["excluded"], [])

    def test_a_logo_slot_for_a_vanished_pack_is_dropped(self):
        """Left in, it still counts toward over_capacity for a pack that is gone."""
        previous = {"version": 1, "targets": [["gone", 9]], "known": ["gone"],
                    "excluded": [], "moves": [], "held": [],
                    "logo_slots": {"9": 1, "1": 1}}
        plan = self.merged(previous, [card("here", pack=1)], {"here": 1}, {"here"},
                           live={"here"})
        self.assertEqual(plan["logo_slots"], {"1": 1})
        self.assertEqual(plan["over_capacity"], {})

    def test_a_decision_about_a_live_emoji_outside_the_scope_survives(self):
        """The point of the merge: a partial page must not erase the rest."""
        previous = {"version": 1, "targets": [["elsewhere", 6]],
                    "known": ["elsewhere"], "excluded": [], "moves": [], "held": []}
        plan = self.merged(previous, [card("here", pack=1)], {"here": 1}, {"here"},
                           live={"here", "elsewhere"})
        self.assertEqual(dict(plan["targets"]), {"elsewhere": 6, "here": 1})


if __name__ == "__main__":
    unittest.main()
