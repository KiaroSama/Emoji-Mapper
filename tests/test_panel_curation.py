"""Curation controls operate on the visible plan and keep reversible history."""
from __future__ import annotations

import unittest

from tests import _panel_browser_fixtures as fx

H = fx.Harness()


def setUpModule():
    H.start()


def tearDownModule():
    H.stop()


class CurationControls(unittest.TestCase):
    def open(self, items=None, **kwargs):
        if items is None:
            from emojikit.catalog import Catalog
            with Catalog(H.db) as cat:
                cat.set_inclusion(set())
                cat.set_order(sorted(it.content_key for it in cat.all_items()))
        self.errors = []
        page = H.open(items=items, on_error=self.errors.append, cleanup=self.addCleanup, **kwargs)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        page.set_default_timeout(5000)
        return page

    def test_toolbar_does_not_cover_brand_or_save_at_desktop_widths(self):
        page = self.open(fx.synth(12))
        for width in (1920, 1200):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                brand = page.locator("header h1").bounding_box()
                toolbar = page.locator("header .actions").bounding_box()
                save = page.locator("header .hright").bounding_box()
                self.assertLessEqual(brand["x"] + brand["width"], toolbar["x"])
                self.assertLessEqual(toolbar["x"] + toolbar["width"], save["x"])

    def test_shift_hold_unhold_and_history_restore_the_whole_gesture(self):
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        page.click("#selmode")
        self.assertEqual(page.locator(".tick:visible").count(), 0)
        page.locator(".card .pick").nth(1).click()
        page.locator(".card .pick").nth(4).click(modifiers=["Shift"])
        self.assertEqual(page.locator(".card.picked").count(), 4)
        page.click("#toHold")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 4)
        self.assertEqual(page.locator("#grid .card").count(), 8)
        self.assertEqual(page.locator(".packsep .n").first.inner_text(), "#1–#2")
        page.click("#undo")
        self.assertEqual(page.locator("#grid .card").count(), 12)
        self.assertEqual(page.locator(".card.picked").count(), 4)
        page.click("#redo")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 4)
        page.get_by_role("button", name="Unhold all", exact=True).click()
        self.assertEqual(page.locator("#grid .card").count(), 12)
        self.assertEqual(page.locator("#holdCards .hcard").count(), 0)
        page.click("#undo")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 4)
        page.locator("#holdCards .hcard button").first.click()
        self.assertEqual(page.locator("#holdCards .hcard").count(), 3)

    def test_several_held_emoji_are_picked_and_come_back_in_one_gesture(self):
        """The tray had no pick box, so held emoji could only move one at a time.

        The pick box lives on a grid card, and a held emoji has no grid card --
        so selection mode reached everything except the one place the owner was
        collecting emoji in.
        """
        page = self.open(fx.synth(12, packs=[1] * 12))
        page.click("#selmode")
        page.locator(".card .pick").nth(0).click()
        page.locator(".card .pick").nth(4).click(modifiers=["Shift"])
        page.click("#toHold")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 5)
        self.assertEqual(page.locator("#holdCards .hcard .pick").count(), 5)

        # Click plus shift-click inside the TRAY takes a run of held emoji.
        page.locator("#holdCards .hcard .pick").nth(0).click()
        page.locator("#holdCards .hcard .pick").nth(3).click(modifiers=["Shift"])
        self.assertEqual(page.locator("#holdCards .hcard.picked").count(), 4)
        self.assertEqual(page.locator("#selLabel").inner_text(), "Selection: 4 picked")

        # Unhold on one picked card returns every picked one; the fifth stays.
        page.locator("#holdCards .hcard.picked button").first.click()
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)
        self.assertEqual(page.locator("#grid .card").count(), 11)

    def test_a_held_emoji_joins_the_pack_it_is_dropped_into(self):
        """The panel states the INTENDED layout; it is not a mirror of Telegram.

        The owner drags an emoji into the pack they want it to end up in, saves,
        and the real move happens afterwards from the written plan. So the drop
        is accepted AND re-stamps the emoji into the destination pack. Without
        the stamp the grid re-groups it by the pack it arrived carrying, it
        becomes a one-card run still labelled with the pack it came from, and
        the drag reads as having snapped back -- which is what was reported.
        """
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        page.click("#selmode")
        page.locator(".card .pick").nth(0).click()
        page.click("#toHold")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)

        moved = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const from = it.pack;
                const other = packStarts().starts.find(s => s.pack !== from);
                // What `dragstart` does on the real path: without it the drop
                // leaves no history entry and Undo pops the hold instead.
                remember();
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(other.index + 1, 0, it);
                holdDragKeys = new Set([it.key]);
                const accepted = acceptHeldDrop();
                return {accepted, from, into: other.pack, now: it.pack,
                        included: it.included,
                        assigned: assignedPacks().get(it.key),
                        runs: packStarts().starts.map(s => s.pack)};
            }""")
        self.assertTrue(moved["accepted"], "moving between packs is what the panel is for")
        self.assertTrue(moved["included"], "the emoji must land in the grid, not stay held")
        self.assertEqual(moved["now"], moved["into"], "it must carry the destination pack")
        self.assertEqual(moved["assigned"], moved["into"])
        # The destination run ABSORBED it. A stray island here would be the
        # original defect: the same pack number appearing twice in the runs.
        self.assertEqual(moved["runs"], [moved["from"], moved["into"]])
        self.assertEqual(page.locator("#holdCards .hcard").count(), 0)

        # Undo has to take the stamp back too, or the move looks reverted on
        # screen while the emoji still claims the pack it was moved into.
        page.click("#undo")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1,
                         "undo puts the emoji back in the tray")
        self.assertEqual(
            page.evaluate("() => ITEMS.find(x => !x.included && !x.isLogo).pack"),
            moved["from"], "undo must roll the pack stamp back as well")

    def test_a_full_destination_pack_refuses_the_drop_and_points_at_the_tray(self):
        """Packs are fixed 200-emoji buckets, so a full one takes nothing.

        Making room is what the holding tray is for -- park one of the pack's
        own emoji, then bring the replacement in -- so the refusal names that
        gesture instead of only saying no. The capacity question is asked about
        the DESTINATION: asking about the emoji's own pack (the earlier bug)
        refused moves out of a full pack and allowed moves into one.
        """
        page = self.open(fx.synth(202, packs=[1] * 2 + [2] * 200, excluded=[0]))
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)
        refused = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const full = packStarts().starts.find(s => s.pack !== it.pack);
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(full.index + 1, 0, it);
                let said = null; const real = toast; toast = m => { said = m; };
                holdDragKeys = new Set([it.key]);
                const accepted = acceptHeldDrop();
                toast = real;
                return {accepted, said, held: !it.included, into: full.pack, pack: it.pack};
            }""")
        self.assertFalse(refused["accepted"], "a full pack must take nothing")
        self.assertTrue(refused["held"], "the emoji stays in the tray")
        self.assertEqual(refused["pack"], 1, "a refused drop must not re-stamp it")
        self.assertIn(f"Pack {refused['into']} is full (200/200)", refused["said"])
        self.assertIn("Hold one of its emoji first", refused["said"])

    def test_unhold_all_refuses_a_full_original_pack_without_partial_changes(self):
        items = fx.synth(203, packs=[1] * 201 + [2] * 2, excluded=[195, 202])
        page = self.open(items)
        page.get_by_role("button", name="Unhold all", exact=True).click()
        self.assertEqual(page.locator("#holdCards .hcard").count(), 2)
        self.assertIn("Pack 1 is full (200/200)", page.locator("#toast").inner_text())
        page.locator("#holdCards .hcard button").last.click()
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)

    def test_reset_uses_the_submitted_save_and_is_itself_undoable(self):
        page = self.open(fx.synth(6), clock=True)
        page.click("#anim")
        page.click("#bg")
        page.click("#selmode")
        page.locator(".card .pick").first.click()
        page.click("#toHold")
        page.click("#save")
        page.wait_for_function("__sent('/api/save').length===1")
        expected = page.evaluate("snapshot()")
        page.locator(".card .pick").first.click()
        page.click("#toHold")
        page.click("#anim")
        page.evaluate("__settle(0,200)")
        page.clock.run_for(50)
        self.assertTrue(page.evaluate("selDirty()"))
        edited = page.evaluate("snapshot()")
        page.click("#resetAll")
        self.assertEqual(page.evaluate("snapshot()"), expected)
        page.click("#undo")
        self.assertEqual(page.evaluate("snapshot()"), edited)
        page.click("#redo")
        self.assertEqual(page.evaluate("snapshot()"), expected)

    def test_hold_save_and_reload_use_the_real_server_and_catalog(self):
        page = self.open()
        page.evaluate("window.__net.passthrough=true")
        page.click("#selmode")
        held_key = page.locator(".card").first.get_attribute("data-key")
        page.locator(".card .pick").first.click()
        page.click("#toHold")
        page.click("#save")
        page.wait_for_function("pendingSel===null && !selDirty()")
        self.assertEqual(H.excluded_in_db(), {held_key})
        page.reload(wait_until="load")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)
        self.assertEqual(page.locator(f'#grid .card[data-key="{held_key}"]').count(), 0)

    def test_native_drag_history_and_unhold_restore_the_original_slot(self):
        items = fx.synth(6, packs=[1] * 6)
        page = self.open(items)
        page.click("#selmode")
        page.locator(".card .pick").nth(1).click()
        page.click("#toHold")
        first = page.locator("#grid .card").first
        first.drag_to(page.locator("#grid .card").last)
        self.assertEqual(page.locator("#grid .card").first.get_attribute("data-key"), items[2]["key"])
        page.click("#undo")
        self.assertEqual(page.locator("#grid .card").first.get_attribute("data-key"), items[0]["key"])
        page.click("#redo")
        page.locator("#holdCards .hcard button").click()
        self.assertEqual(page.locator("#grid .card").nth(1).get_attribute("data-key"), items[1]["key"])


if __name__ == "__main__":
    unittest.main()
