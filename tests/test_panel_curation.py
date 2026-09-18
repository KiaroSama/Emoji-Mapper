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

    def test_a_published_emoji_cannot_be_dragged_into_another_pack(self):
        """Telegram has no move-between-sets call: it would be delete + re-add,
        which mints a NEW custom_emoji_id and breaks every stored reference.

        The drop used to be accepted and then silently re-grouped by the item's
        own pack field, so the card snapped back with no explanation -- which
        the owner reported as the drag being broken.
        """
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        page.click("#selmode")
        page.locator(".card .pick").nth(0).click()
        page.click("#toHold")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)

        refused = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const other = packStarts().starts.find(s => s.pack !== it.pack);
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(other.index + 1, 0, it);
                let said = null; const real = toast; toast = m => { said = m; };
                holdDragKeys = new Set([it.key]);
                const accepted = acceptHeldDrop();
                toast = real;
                return {accepted, said, held: !it.included, from: it.pack, into: other.pack};
            }""")
        self.assertFalse(refused["accepted"], "a cross-pack drop must not be accepted")
        self.assertTrue(refused["held"], "the emoji must stay held, not land in the wrong pack")
        self.assertIn(f"pack {refused['into']}", refused["said"])
        self.assertIn(str(refused["from"]), refused["said"])

        # The same gesture INSIDE its own pack still works -- the refusal must
        # not cost the move that was always supported.
        accepted = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const own = packStarts().starts.find(s => s.pack === it.pack);
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(own.index + 1, 0, it);
                holdDragKeys = new Set([it.key]);
                return {accepted: acceptHeldDrop(), included: it.included};
            }""")
        self.assertTrue(accepted["accepted"])
        self.assertTrue(accepted["included"])
        self.assertEqual(page.locator("#holdCards .hcard").count(), 0)

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
