"""The move plan the panel writes for whoever rearranges the real packs."""

import json
import tempfile
import unittest
from pathlib import Path

from emojikit.panel_plan import PLAN_NAME, build_plan, write_plan


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


if __name__ == "__main__":
    unittest.main()
