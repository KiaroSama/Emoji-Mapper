"""Curation controls operate on the visible plan and keep reversible history."""
from __future__ import annotations

import unittest

from tests import _panel_browser_fixtures as fx

H = fx.Harness()


def setUpModule():
    H.start()


def tearDownModule():
    H.stop()


class PanelPage:
    """The shared `open`, and deliberately NOT a TestCase.

    Every class below used to inherit from `CurationControls`, which IS one, so
    unittest collected its nine tests again for each subclass: ten new tests ran
    as thirty-seven. A mixin shares the fixture without sharing the suite.
    """

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


class CurationControls(PanelPage, unittest.TestCase):
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
                holdDragKeys = new Set([it.key]);
                carried.clear(); carried.add(it.key);
                // And what `dragover` does. The pack comes from the card the
                // pointer is ON, so a test that skips this is testing a drop
                // that cannot happen -- nothing was aimed at, so by design
                // nothing is stamped.
                aimAt(other.index);
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(other.index + 1, 0, it);
                const accepted = acceptDrop();
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
                holdDragKeys = new Set([it.key]);
                carried.clear(); carried.add(it.key);
                aimAt(full.index);               // the pointer was over the full pack
                ITEMS.splice(ITEMS.indexOf(it), 1);
                ITEMS.splice(full.index + 1, 0, it);
                let said = null; const real = toast; toast = m => { said = m; };
                const accepted = acceptDrop();
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


class TheHoldTrayIsSelectableByHand(PanelPage, unittest.TestCase):
    """The tray's selection was reachable only by hitting a 1.3em box on a 64px
    card -- about 17px -- so a click on the card did nothing and multi-select,
    with the multi-card drag that depends on it, both looked broken while the
    logic underneath was correct and measurably worked when driven directly."""

    def held(self, page, count=5):
        page.click("#selmode")
        page.evaluate(
            "n => holdKeys(new Set(ITEMS.filter(x=>!x.isLogo && x.included)"
            ".slice(0, n).map(x=>x.key)))", count)
        page.wait_for_timeout(200)
        return page.locator("#holdCards .hcard")

    def ticks(self, page):
        """What each held card's pick box actually renders."""
        return page.evaluate(
            "[...document.querySelectorAll('#holdCards .hcard')].map("
            "c => getComputedStyle(c.querySelector('.pick'), '::after').content)")

    def test_an_unselected_tick_is_empty_and_only_a_selected_one_shows_a_check(self):
        """The reported defect: both states rendered the same glyph and only the
        colour changed, so an unselected box looked checked."""
        page = self.open(fx.synth(12))
        cards = self.held(page)
        self.assertTrue(all(t == "none" for t in self.ticks(page)),
                        f"a tick was drawn with nothing selected: {self.ticks(page)}")
        cards.nth(1).locator("img").click()
        page.wait_for_timeout(150)
        drawn = [i for i, t in enumerate(self.ticks(page)) if t != "none"]
        self.assertEqual(drawn, [1], "exactly the selected card shows a check")

    def test_clicking_a_cards_face_selects_it_and_shift_takes_the_range(self):
        page = self.open(fx.synth(12))
        cards = self.held(page)
        cards.nth(0).locator("img").click()      # the artwork, NOT the pick box
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("picked.size"), 1)
        cards.nth(3).locator("img").click(modifiers=["Shift"])
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("picked.size"), 4,
                         "Shift must take the whole inclusive range")

    def test_one_click_on_the_box_toggles_once_not_twice(self):
        """The box is a child of the card. Two handlers for one gesture would
        toggle it and untoggle it, netting to zero."""
        page = self.open(fx.synth(12))
        cards = self.held(page)
        cards.nth(0).locator(".pick").click()
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("picked.size"), 1)

    def test_unhold_does_not_disturb_the_selection(self):
        page = self.open(fx.synth(12))
        cards = self.held(page)
        cards.nth(0).locator("img").click()
        cards.nth(2).locator("img").click()
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("picked.size"), 2)
        page.locator("#holdCards .hcard button").nth(4).click()
        page.wait_for_timeout(250)
        self.assertEqual(page.evaluate("picked.size"), 2,
                         "unholding an unpicked card changed the selection")

    def test_outside_selection_mode_a_click_selects_nothing(self):
        page = self.open(fx.synth(12))
        cards = self.held(page)
        page.click("#selmode")                   # back off again
        page.wait_for_timeout(150)
        cards.nth(0).locator("img").click()
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("picked.size"), 0)

    def test_a_drag_from_the_tray_carries_every_selected_emoji(self):
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        cards = self.held(page, 3)
        cards.nth(0).locator("img").click()
        cards.nth(2).locator("img").click(modifiers=["Shift"])
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("heldPicked().length"), 3)
        # A real DataTransfer: a synthetic dragstart has none, and the handler
        # sets `effectAllowed` on it before anything else.
        transfer = page.evaluate_handle("() => new DataTransfer()")
        cards.nth(0).dispatch_event("dragstart", {"dataTransfer": transfer})
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("holdDragKeys.size"), 3,
                         "the drag must carry the whole selection, not one card")

    def test_dragging_an_unselected_held_card_carries_only_itself(self):
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        cards = self.held(page, 3)
        cards.nth(0).locator("img").click()
        page.wait_for_timeout(150)
        transfer = page.evaluate_handle("() => new DataTransfer()")
        cards.nth(2).dispatch_event("dragstart", {"dataTransfer": transfer})
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("holdDragKeys.size"), 1)


class ADroppedEmojiJoinsThePackItWasAimedAt(PanelPage, unittest.TestCase):
    """Where a card comes to REST is not where the owner aimed.

    Measured against the sandbox before this class existed: a held emoji
    dropped on pack 4's FIRST card landed in pack 3, because the destination
    was read by scanning backwards from the resting place -- and at a boundary
    the card above the resting place is always the previous pack. Worse, a
    grid-to-grid drag never re-stamped at all: an emoji carried from pack 1
    into the middle of pack 4 kept pack 1 and CUT PACK 4 IN TWO.

    Both gestures are driven here the way the page drives them: `aimAt` is what
    `dragover` calls, `moveCarried` is what moves the model, and `acceptDrop`
    is what the drop handler calls. Only the pointer is synthetic.
    """

    def hold_first(self, page):
        page.click("#selmode")
        page.locator(".card .pick").nth(0).click()
        page.click("#toHold")
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1)

    def test_a_drop_on_a_packs_first_card_joins_that_pack_not_the_one_above(self):
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        self.hold_first(page)
        moved = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const head = packStarts().starts.find(s => s.pack === 2).index;
                remember();
                holdDragKeys = new Set([it.key]);
                carried.clear(); carried.add(it.key);
                aimAt(head);            // the pointer is ON pack 2's first card
                moveCarried(head);      // dropped on its left half: before it
                const accepted = acceptDrop();
                return {accepted, pack: it.pack, at: ITEMS.indexOf(it),
                        head: packStarts().starts.find(s => s.pack === 2).index,
                        runs: packStarts().starts.map(s => s.pack)};
            }""")
        self.assertTrue(moved["accepted"])
        self.assertEqual(moved["pack"], 2, "aimed at pack 2, so it joins pack 2")
        # Not merely stamped: it must be pack 2's FIRST emoji, which is where it
        # was dropped. Landing in pack 1 was a stamp of the pack above.
        self.assertEqual(moved["at"], moved["head"])
        self.assertEqual(moved["runs"], [1, 2], "no run may be cut by a drop")

    def test_a_grid_to_grid_move_re_stamps_and_does_not_split_the_pack(self):
        """The half nobody reported, and the more destructive one."""
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        moved = page.evaluate(
            """() => {
                const it = ITEMS.find(x => x.pack === 1 && x.included && !x.isLogo);
                const mid = packStarts().starts.find(s => s.pack === 2).index + 2;
                dragKey = it.key; dragSnap = snapshot(); aimedPack = undefined;
                carried.clear(); carried.add(it.key);
                aimAt(mid); moveCarried(mid);
                const accepted = acceptDrop();
                commitDrag(); endDrag(true);
                return {accepted, pack: it.pack,
                        runs: packStarts().starts.map(s => s.pack)};
            }""")
        self.assertTrue(moved["accepted"])
        self.assertEqual(moved["pack"], 2, "a grid drag must re-stamp too")
        self.assertEqual(moved["runs"], [1, 2],
                         "the destination pack was split by the dropped card")

    def test_a_drop_that_aimed_at_nothing_stamps_nothing(self):
        """Unknown is not a destination.

        A drop can land on the grid without the pointer ever having been over a
        card. Inferring a pack there would re-file an emoji into a pack nobody
        chose -- and the stamp is an instruction the publish step carries out.
        """
        page = self.open(fx.synth(12, packs=[1] * 6 + [2] * 6))
        self.hold_first(page)
        after = page.evaluate(
            """() => {
                const it = ITEMS.find(x => !x.included && !x.isLogo);
                const was = it.pack;
                holdDragKeys = new Set([it.key]);
                carried.clear(); carried.add(it.key);
                aimedPack = undefined;           // no dragover ever fired
                const accepted = acceptDrop();
                return {accepted, was, now: it.pack};
            }""")
        self.assertTrue(after["accepted"], "the emoji still comes out of the tray")
        self.assertEqual(after["now"], after["was"])

    def test_a_drop_into_a_run_with_no_pack_number_invents_no_number(self):
        """A catalog of candidates has no pack numbers at all yet.

        The stale number is set by hand because the view cannot produce that
        state today -- every card in an unnumbered catalog starts without one.
        It is pinned anyway: the destination decides, and a number carried into
        an unnumbered run is exactly what cuts a false run inside a pack that
        has not been published.
        """
        page = self.open(fx.synth(12))          # no packs: pure candidates
        after = page.evaluate(
            """() => {
                const it = ITEMS.find(x => x.included && !x.isLogo);
                it.pack = 7;                     // a stamp from somewhere else
                dragKey = it.key; dragSnap = snapshot(); aimedPack = undefined;
                carried.clear(); carried.add(it.key);
                aimAt(6); moveCarried(6);
                const accepted = acceptDrop();
                endDrag(true);
                return {accepted, aimed: aimedPack, now: it.pack ?? null};
            }""")
        self.assertTrue(after["accepted"])
        self.assertIsNone(after["aimed"], "an unnumbered run has no number to aim at")
        self.assertIsNone(after["now"], "the emoji joins the run, it does not keep a number")


class TheGridIsSelectableByClick(PanelPage, unittest.TestCase):
    """Feature 002 gave the tray a click and left the grid as it was: the only
    way to pick a grid card was a 1.7em box on a 140px card, because the click
    handler returned early for everything else in selection mode."""

    def test_clicking_a_cards_face_selects_it_and_shift_takes_the_range(self):
        page = self.open(fx.synth(12))
        page.click("#selmode")
        page.locator(".card .thumb").nth(1).click()   # the artwork, not the box
        self.assertEqual(page.evaluate("picked.size"), 1)
        page.locator(".card .thumb").nth(4).click(modifiers=["Shift"])
        self.assertEqual(page.evaluate("picked.size"), 4,
                         "Shift must take the whole inclusive range")

    def test_a_click_on_the_box_still_toggles_exactly_once(self):
        """The box has its own `pointerdown`. Acting on the click as well would
        toggle it and untoggle it, netting to zero."""
        page = self.open(fx.synth(12))
        page.click("#selmode")
        page.locator(".card .pick").nth(2).click()
        self.assertEqual(page.evaluate("picked.size"), 1)
        page.locator(".card .pick").nth(2).click()
        self.assertEqual(page.evaluate("picked.size"), 0, "and once back off again")

    def test_outside_selection_mode_a_click_still_toggles_what_ships(self):
        page = self.open(fx.synth(12))
        page.locator(".card .thumb").nth(0).click()
        self.assertEqual(page.locator("#holdCards .hcard").count(), 1,
                         "an unticked emoji goes to the tray, as it always has")
        self.assertEqual(page.evaluate("picked.size"), 0)

    def test_the_brand_logo_is_never_picked(self):
        page = self.open(fx.synth(6, logo=True))
        page.click("#selmode")
        page.locator(".card.logo").click()
        self.assertEqual(page.evaluate("picked.size"), 0)


class APickedCardWearsAMovingRing(PanelPage, unittest.TestCase):
    """The owner's report: a selected card and an unselected one differed by a
    dark inset outline, among four format accent colours, and could not be told
    apart at a glance."""

    def ring(self, page, selector):
        return page.evaluate(
            "s => {const n = document.querySelector(s); return n ? "
            "{anim: getComputedStyle(n, '::before').animationName, "
            " play: n.getAnimations({subtree: true}).map(a => a.playState)} : null}",
            selector)

    def test_only_a_picked_card_carries_the_ring_and_it_runs(self):
        page = self.open(fx.synth(12))
        page.click("#selmode")
        page.locator(".card .thumb").nth(0).click()
        marked = self.ring(page, ".card.picked")
        plain = self.ring(page, ".card:not(.picked)")
        self.assertEqual(marked["anim"], "pickspin")
        self.assertIn("running", marked["play"], "a still ring is not a moving one")
        self.assertEqual(plain["anim"], "none")

    def test_a_picked_held_card_carries_the_same_ring(self):
        """One control, one appearance. Two markers for one state would be the
        defect this replaces, not an improvement on it."""
        page = self.open(fx.synth(12))
        page.click("#selmode")
        page.locator(".card .pick").nth(0).click()
        page.click("#toHold")
        page.locator("#holdCards .hcard img").click()
        page.wait_for_timeout(150)
        self.assertEqual(self.ring(page, ".hcard.picked")["anim"], "pickspin")
