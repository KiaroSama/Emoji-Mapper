"""Assertions against the page the panel serves (``panel.PAGE``).

Drag-and-drop, undo/redo, the viewport observer and the pack separators all
live in the page's own JavaScript, so there is no Python function to call --
the served document is the only level at which the behaviour exists. Every
one of these pins a bug that was actually reported.
"""

from __future__ import annotations

import re
import sys
import unittest

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

import panel as p


class DragAndDropOrdering(unittest.TestCase):
    """Two reported bugs, pinned at the only level available: the served page.

    The reorder itself lives in a drop handler, so there is no Python function
    to call. What these assert is exactly what regressed.
    """

    def test_the_drop_commits_the_preview_instead_of_recomputing_it(self):
        """Two bugs came from drop-time index arithmetic; there is none now.

        It once fell back to `ITEMS.length-1` when the release was not on a
        card, so a drop in a grid gap threw the emoji to the end. Then it used
        `splice(from,1)` followed by `splice(to,0)` with a `to` measured BEFORE
        the removal -- which lands the card one slot past the tile you aimed at
        when dragging DOWN and exactly on it when dragging UP.

        The drag now moves the real node, so the grid IS the proposal and the
        drop only adopts it. No index is derived at drop time, so neither bug
        has anywhere to live.
        """
        page = p.PAGE
        self.assertNotIn("card ? ITEMS.findIndex(x=>x.key===card.dataset.key) "
                         ": ITEMS.length-1", page)
        drop = page[page.index("grid.addEventListener('drop'"):]
        drop = drop[:drop.index("grid.addEventListener('dragend'")]
        self.assertIn("commitDrag()", drop)
        for arithmetic in ("findIndex", "splice", "ITEMS.length"):
            self.assertNotIn(arithmetic, drop,
                             f"the drop handler must not reach for {arithmetic}")
        commit = page[page.index("function commitDrag(){"):]
        commit = commit[:commit.index("function endDrag")]
        self.assertIn("grid.querySelectorAll('.card')", commit,
                      "the committed order must be read off the DOM preview")

    def test_a_cancelled_drag_puts_the_card_back(self):
        """The preview mutates the DOM, so ITEMS and the grid disagree until
        the drop. An escape or a drop outside must undo it, or the page shows
        an order that is not the one it would save."""
        page = p.PAGE
        start = page[page.index("grid.addEventListener('dragstart'"):]
        self.assertIn("dragHome=card.nextSibling", start[:start.index("});")])
        end = page[page.index("function endDrag(committed){"):]
        end = end[:end.index("// --- Auto-scroll")]
        self.assertIn("if(!committed", end)
        self.assertIn("grid.insertBefore(node", end)

    def test_the_preview_side_decides_before_or_after(self):
        """Without it the last slot of a row is unreachable: every hover would
        mean "before this card"."""
        page = p.PAGE
        over = page[page.index("grid.addEventListener('dragover'"):]
        over = over[:over.index("grid.addEventListener('drop'")]
        self.assertIn("getBoundingClientRect", over)
        self.assertIn("r.left + r.width/2", over)
        self.assertIn("card.nextSibling", over)

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
            ("drag reorder", "function commitDrag(){", "ITEMS.length = 0"),
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
        # The drag affordance must not be any format's colour, or the card
        # being carried reads as "this one is animated".
        drag = page[page.index(".card.drag{"):]
        drag = drag[:drag.index("}")]
        for fmt, c in colours.items():
            self.assertNotIn(c, drag, f"the dragged card uses the {fmt} colour")

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

    def test_the_tick_offers_a_click_not_a_grab(self):
        """The card is cursor:grab because it is the drag handle, and cursor
        inherits -- so the switch you are aiming at offered a hand for a drag."""
        page = p.PAGE
        tick = page[page.index(".tick{"):]
        self.assertIn("cursor:pointer", tick[:tick.index("}")])

    def test_scrolling_holds_every_card_on_frame_zero(self):
        """The one moment the decoding is pure waste: the frames go past too
        fast to read while the compositor is already busy."""
        page = p.PAGE
        self.assertIn("function freezeAll(){", page)
        scroll = page[page.index("addEventListener('scroll'"):]
        scroll = scroll[:scroll.index("}, {passive:true});")]
        self.assertIn("freezeAll()", scroll)
        self.assertIn("applyAnim()", scroll, "it must thaw again when you stop")
        self.assertIn("passive", page[page.index("addEventListener('scroll'"):][:400],
                      "a non-passive scroll listener blocks the scroll it watches")
        # The band beyond the viewport animated a row nobody was looking at.
        self.assertIn("rootMargin: '0px'", page)
        self.assertNotIn("rootMargin: '120px'", page)

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


class ThePanelPageActuallyShips(unittest.TestCase):
    """The page moved out of panel.py into assets/panel.html.

    Every other test in this module reads `p.PAGE` and so would still pass if
    the asset were missing from a checkout -- the import would simply blow up
    first, somewhere unrelated. That is not hypothetical: the brand logo was an
    absolute `F:\\` path once, so on every machine but one the "mandatory" logo
    silently vanished and nothing failed. Same shape, same guard.
    """

    def test_the_page_is_a_real_file_inside_the_repo(self):
        asset = p.ASSET_DIR / "panel.html"
        self.assertTrue(asset.is_file(), f"the panel page is missing: {asset}")
        self.assertTrue(
            str(asset.resolve()).startswith(str(ROOT.resolve())),
            "the page must ship in the repo, not point at a machine-specific path")

    def test_the_loaded_page_is_the_document_the_handler_expects(self):
        """Loaded, not just present: an empty or truncated file must not pass."""
        self.assertTrue(p.PAGE.startswith("<!doctype html>"))
        self.assertTrue(p.PAGE.rstrip().endswith("</html>"))
        # Every placeholder the handler substitutes must survive extraction --
        # a page missing one renders the literal token to the browser.
        for token in ("__ITEMS__", "__TOKEN__", "__PREVIEW_FPS__",
                      "__PER_SET__", "__HIDDEN__"):
            self.assertIn(token, p.PAGE, f"{token} lost in the asset")


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
