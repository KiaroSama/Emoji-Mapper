"""Assertions against what the panel serves: the page (``panel.PAGE``) and its
scripts (``panel.SCRIPT``, the files concatenated in load order).

Drag-and-drop, undo/redo, the virtual grid, zoom, the viewport observers and
the pack separators all live in the page's own JavaScript, so there is no
Python function to call. These served-text contracts supplement the browser
tests, which verify the actual gestures, timing, layout and persisted results.
"""

from __future__ import annotations

import re
import sys
import unittest

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

from emojikit import panel as p

PAGE = p.PAGE
SCRIPT = p.SCRIPT


def block(src: str, start: str, end: str) -> str:
    """The text from the first ``start`` up to the next ``end`` after it."""
    i = src.index(start)
    return src[i:src.index(end, i)]


class DragAndDropOrdering(unittest.TestCase):
    """The model is edited AS you drag; the drop only keeps what it finds."""

    def test_the_drop_keeps_the_model_instead_of_recomputing_an_index(self):
        """Two bugs came from drop-time index arithmetic; there is none now.

        It once fell back to `ITEMS.length-1` when the release was not on a
        card, so a drop in a grid gap threw the emoji to the end. Then it used
        `splice(from,1)` followed by `splice(to,0)` with a `to` measured BEFORE
        the removal -- which lands the card one slot past the tile you aimed at
        when dragging DOWN and exactly on it when dragging UP.

        Every dragover now moves the carried items inside ITEMS and re-projects
        the grid, so the translucent tile IS where they land. The drop compares
        the order with the snapshot taken at dragstart and records it.
        """
        drop = block(SCRIPT, "grid.addEventListener('drop'", "grid.addEventListener('dragend'")
        self.assertIn("commitDrag()", drop)
        for arithmetic in ("findIndex", "splice", "ITEMS.length"):
            self.assertNotIn(arithmetic, drop, f"the drop handler must not reach for {arithmetic}")
        commit = block(SCRIPT, "function commitDrag(){", "function endDrag")
        self.assertIn("order.every((k,i)=>k===dragSnap.order[i])", commit)
        self.assertIn("selSig(ITEMS.filter(x=>x.included).map(x=>x.key))===selSig(dragSnap.included)",
                      commit,
                      "a tray release changes inclusion even when its position stays the same")
        self.assertIn("packs===JSON.stringify(dragSnap.packs)) return;", commit,
                      "and a cross-pack drop changes the stamp even when the order holds")
        self.assertLess(commit.index("remember(dragSnap);"), commit.index("saveOrder();"))

    def test_a_cancelled_drag_restores_the_order_taken_at_dragstart(self):
        """The model moves with the pointer, so an escape or a drop outside
        must put the dragstart order back, or the page shows an order that is
        not the one it saved."""
        start = block(SCRIPT, "grid.addEventListener('dragstart'", "grid.addEventListener('dragover'")
        self.assertIn("dragSnap=snapshot();", start)
        end = block(SCRIPT, "function endDrag(committed){", "// --- Auto-scroll")
        self.assertIn("if(!committed && dragKey !== null){ applySnapshot(dragSnap,false); }", end)

    def test_the_pointer_side_decides_before_or_after(self):
        """Without it the last slot of a row is unreachable: every hover would
        mean "before this card"."""
        over = block(SCRIPT, "grid.addEventListener('dragover'", "function moveCarried")
        self.assertIn("getBoundingClientRect", over)
        self.assertIn("(e.clientX > r.left + r.width/2) ? j + 1 : j", over)

    def test_hovering_the_slot_the_run_already_holds_changes_nothing(self):
        """dragover fires continuously; a relayout per event that moves nothing
        would be the old full-grid cost back under another name."""
        move = block(SCRIPT, "function moveCarried(slot){", "grid.addEventListener('drop'")
        self.assertIn("if(next.every((x,i)=>x===ITEMS[i])) return;", move)
        self.assertLess(move.index("return;"), move.index("relayout();"))

    def test_dragging_to_an_edge_scrolls_the_page(self):
        """Without this the drag is trapped in the current viewport.

        With 200 cards there is otherwise no way to carry #200 up to #10.
        """
        self.assertIn("function edgeScroll(", SCRIPT)
        self.assertIn("requestAnimationFrame", SCRIPT)
        # On the document: at the top of the window the pointer sits over the
        # sticky header, where a grid-only listener never fires.
        doc_over = SCRIPT.index("document.addEventListener('dragover'")
        self.assertIn("edgeScroll(e.clientY)", SCRIPT[doc_over:doc_over + 300])

    def test_a_carried_card_is_parked_never_unmounted(self):
        """The browser delivers dragend to the SOURCE node. A carried card whose
        row scrolls out of the window used to be a candidate for removal, and a
        removed source leaves the gesture stuck with no drop and no dragend."""
        retire = block(SCRIPT, "function retire(node){", "function relayout")
        self.assertIn("if(carried.has(node.dataset.key)) park.appendChild(node);", retire)
        self.assertIn("else unmountCard(node);", retire)
        self.assertIn('id="park"', PAGE)
        # And the park is emptied once nothing carries them any more.
        end = block(SCRIPT, "function endDrag(committed){", "// --- Auto-scroll")
        self.assertIn("while(park.firstChild) unmountCard(park.firstChild);", end)

    def test_the_position_number_is_projected_not_stored(self):
        """A number written at build time is right once and wrong after a drag.

        render() writes the index of every mounted card each time the grid is
        projected -- ITEMS *is* the order, so there is no second copy to drift.
        """
        card = block(SCRIPT, "function makeCard(it){", "function packStarts")
        self.assertIn("hdr.appendChild(el('span','pos',''));", card)
        self.assertNotIn("el('span','pos', n", SCRIPT)   # never filled at build time
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertIn("if(pos.textContent !== String(displayPos[k])) pos.textContent = displayPos[k];", render)

    def test_the_logo_IS_numbered_because_it_takes_a_real_slot(self):
        """It leads every set it is added to, so it costs one of the 200.

        build_collection reserves it -- `capacity = per_set - 1` -- so leaving
        it out of the panel's numbering made the panel disagree with what ships:
        the owner read "200" and the pack was 201.
        """
        hdr = block(SCRIPT, "const hdr = el('div','hdr');", "card.appendChild(hdr);")
        self.assertIn("hdr.appendChild(el('span','pos',''));", hdr)
        # The tick is the one thing the logo does NOT get: it is not toggleable.
        self.assertIn("if(!it.isLogo) hdr.appendChild(el('span','tick'", hdr)
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertNotIn("isLogo", render, "skipping the logo is what made the count wrong")

    def test_a_selection_that_cannot_be_one_pack_says_so(self):
        """200 chosen emoji plus the logo is 201, over Telegram's per-set cap.

        Surfaced in the header rather than discovered as a surprise second set.

        The count is the number of runs ``packStarts()`` produces -- the same
        function that draws the boundaries -- and NOT a division of the total.
        ``Math.ceil(included / PER_SET)`` counted the logo once for the whole
        selection when every pack is led by one, so 399 emoji plus a logo was
        reported as two packs over a grid already drawing three. That the two
        agree at every boundary is asserted in ``tests/test_panel_browser.py``.
        """
        count = block(SCRIPT, "function updateCount(){", chr(10) + "}")
        self.assertIn("packStarts().starts.length", count)
        self.assertNotIn("Math.ceil(included / PER_SET)", SCRIPT,
                         "a second, wrong copy of the pack planner")
        self.assertIn("PER_SET - (logo ? 1 : 0)", count, "every pack is led by the logo")
        # The limit comes from build_collection, not a second copy that drifts.
        self.assertIn("__PER_SET__", PAGE)
        self.assertEqual(p.PER_SET, 200)

    def test_the_card_header_cannot_overlap_itself(self):
        """The badge, the number and the tick shared one ~120px strip.

        Absolutely positioned at left / centre / right, "animated" ran straight
        under the number and both were unreadable. They are now a centred
        COLUMN -- number over format -- with only the tick pinned to the corner,
        which is what keeps it from pushing the stack off centre.
        """
        rule = block(PAGE, ".hdr{", "}")
        self.assertIn("display:flex", rule)
        self.assertIn("flex-direction:column", rule)
        self.assertIn("align-items:center", rule)
        # In the column flow, so they cannot be placed on top of each other.
        for cls in (".badge{", ".pos{"):
            self.assertNotIn("position:absolute", block(PAGE, cls, "}"), cls)
        # The tick is the one exception, and deliberately so.
        self.assertIn("position:absolute", block(PAGE, ".tick{", "}"))

    def test_the_scroll_loop_cannot_outlive_the_drag(self):
        """A drag released outside the window fires no drop.

        An unguarded rAF loop would then scroll the page forever.
        """
        step = SCRIPT[SCRIPT.index("function stepEdge("):]
        self.assertIn("if(dragKey === null || !edgeSpeed) return;", step[:step.index("}") + 400])
        self.assertIn("function stopEdgeScroll(", SCRIPT)
        self.assertIn("cancelAnimationFrame", SCRIPT)


class UndoRedoAndFormatColours(unittest.TestCase):
    """The header controls, the history stack, and telling formats apart."""

    def test_every_mutation_records_the_state_to_return_to(self):
        """remember() must hold the state from BEFORE the change, at all three
        sites. A drag mutates from its first dragover, so it records the
        snapshot taken at dragstart rather than one taken at the drop.

        A missed site is invisible until someone undoes past it and gets the
        wrong state back, which is worse than having no undo at all.
        """
        for label, marker, end, mutation, call in (
            ("select all / invert", "function setAll(fn){", "// Reduced motion", "setIncluded(it, fn(it))", "remember()"),
            ("card toggle", "grid.addEventListener('click'", "// --- Losing the server", "setIncluded(ITEMS[i], !ITEMS[i].included)", "remember()"),
            ("drag reorder", "function commitDrag(){", "function endDrag", "saveOrder();", "remember(dragSnap)"),
        ):
            body = block(SCRIPT, marker, end)
            self.assertLess(body.index(call), body.index(mutation), f"{label} does not record history first")

    def test_the_history_is_bounded(self):
        """A long curation session must not grow the stack without limit."""
        self.assertIn("HISTORY_MAX", SCRIPT)
        body = SCRIPT[SCRIPT.index("function remember(snap){"):]
        self.assertIn("past.shift()", body[:body.index("}") + 200])

    def test_a_new_action_drops_the_redo_branch(self):
        body = SCRIPT[SCRIPT.index("function remember(snap){"):]
        self.assertIn("future.length = 0", body[:body.index("updateHistoryButtons")])

    def test_undo_re_projects_the_grid_instead_of_rebuilding_it(self):
        """A rebuild would re-request every thumbnail and preview on screen.

        relayout() reconciles the mounted nodes: a node already in the document
        RELOCATES on insertBefore, so the loaded media survives an undo.
        """
        body = block(SCRIPT, "function applySnapshot(", "function undo()")
        self.assertIn("relayout();", body)
        self.assertIn("saveOrder()", body, "order auto-saves, so an undone reorder must reach the catalog")
        self.assertNotIn("cards.clear()", SCRIPT, "nothing may throw the mounted cards away wholesale")
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertIn("if(n === cur){ cur = cur.nextSibling; continue; }", render)
        self.assertIn("grid.insertBefore(n, cur);", render)

    def test_each_format_has_its_own_accent(self):
        colours = {}
        for fmt in ("static", "animated", "video"):
            rule = PAGE[PAGE.index(f".card.fmt-{fmt}"):]
            colours[fmt] = rule[rule.index("--fmt:") + 6:rule.index(";")]
        self.assertEqual(len(set(colours.values())), 3, colours)
        # The drag affordance must not be any format's colour, or the card
        # being carried reads as "this one is animated".
        drag = block(PAGE, ".card.drag{", "}")
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
        rule = block(PAGE, ".thumb{", "}")
        self.assertIn("outline:.17em solid var(--fmt", rule)
        self.assertIn("outline-offset:", rule, "without an offset the frame still touches the artwork")
        self.assertNotIn("border:", rule.replace("border-radius", ""),
                         "an inner border shrinks the thumbnail it frames")
        self.assertIn("#22c55e", block(PAGE, ".card.on{", "}"), "the selected frame must be green")

    def test_the_tick_offers_a_click_not_a_grab(self):
        """The card is cursor:grab because it is the drag handle, and cursor
        inherits -- so the switch you are aiming at offered a hand for a drag."""
        self.assertIn("cursor:pointer", block(PAGE, ".tick{", "}"))

    def test_scrolling_holds_every_card_on_frame_zero(self):
        """The one moment the decoding is pure waste: the frames go past too
        fast to read while the compositor is already busy."""
        self.assertIn("function freezeAll(){", SCRIPT)
        scroll = block(SCRIPT, "addEventListener('scroll'", "}, {passive:true});")
        self.assertIn("freezeAll()", scroll)
        self.assertIn("applyAnim()", scroll, "it must thaw again when you stop")
        self.assertIn("scheduleRender()", scroll, "the window follows the scroll")
        self.assertIn("passive", SCRIPT[SCRIPT.index("addEventListener('scroll'"):][:400],
                      "a non-passive scroll listener blocks the scroll it watches")
        # The playback observer has NO margin: a band beyond the viewport
        # animated a row nobody was looking at, above AND below.
        self.assertIn("}, {root: null, rootMargin: '0px'})", block(SCRIPT, "const animIO", "function setPlaying"))

    def test_a_switch_shows_its_own_state(self):
        """On/off shown by the control itself, not only by its label.

        The rule is bound to `.switch`, not to one id: selection mode is a
        second switch and an id-bound rule would have left its knob dead.
        """
        self.assertIn('aria-pressed', PAGE)
        self.assertIn('.switch[aria-pressed="true"]  .knob{background:#22c55e}', PAGE)
        self.assertIn('.switch[aria-pressed="false"] .knob{background:#f43f5e}', PAGE)
        for control in ('id="anim" class="switch"', 'id="selmode" class="switch"'):
            self.assertIn(control, PAGE)
        self.assertIn("--btn:#34ebc6", PAGE)

    def test_the_card_header_stacks_number_over_format(self):
        rule = block(PAGE, ".hdr{", "}")
        self.assertIn("flex-direction:column", rule)
        self.assertIn("align-items:center", rule)
        # The number is appended before the format badge, so it sits on top.
        markup = block(SCRIPT, "const hdr = el('div','hdr');", "card.appendChild(hdr);")
        self.assertLess(markup.index("'pos'"), markup.index("'badge'"))

    def test_only_save_selection_sits_outside_the_centre_group(self):
        actions = block(PAGE, '<div class="actions">', "</div>")
        for btn in ("undo", "redo", "top", "bot", "zoomOut", "zoomReset", "zoomIn",
                    "all", "none", "inv", "bg", "anim", "selmode"):
            self.assertIn(f'id="{btn}"', actions, btn)
        self.assertNotIn('id="save"', actions, "Save writes; it stays out of the centre group")
        # The expanding toolbar must wrap rather than cover the title; the real
        # rendered rectangles are checked in test_panel_curation.
        self.assertIn("grid-template-columns:auto minmax(0,1fr) auto", PAGE)


class OffScreenCostsNothing(unittest.TestCase):
    """A card you cannot see must not be decoding frames or holding a player.

    Four hundred and forty-one of the 1 068 cards are animated and 56 are
    video: if leaving the viewport did not stop them the grid would decode
    every one of them forever, which is what "the page is heavy" first meant.
    """

    def test_leaving_the_viewport_stops_video_AND_animation(self):
        io_block = block(SCRIPT, "const animIO", "}, {root:")
        # One flag decides both media kinds, and it is driven by intersection.
        self.assertIn("e.isIntersecting", io_block)
        self.assertIn("setPlaying(t, live)", io_block, "video must be paused when it scrolls away, not muted")
        self.assertIn("t.dataset.still", io_block, "an animated image must fall back to its single frame")
        # Coming back must restore it -- a one-way stop would leave a dead grid.
        self.assertIn("t.dataset.anim", io_block)

    def test_a_mounted_card_is_observed_and_an_unmounted_one_released(self):
        """mount() creates the nodes, so it is what must start observing; the
        window moves on every scroll, so unmounting must stop it again or the
        observer keeps a reference to every card that ever scrolled past."""
        mount = block(SCRIPT, "function mount(k){", "function unmountCard")
        self.assertIn("animIO.observe(n)", mount)
        self.assertIn("videoIO.observe(n)", mount)
        unmount = block(SCRIPT, "function unmountCard(c){", "const videoIO")
        self.assertIn("animIO.unobserve(n)", unmount)
        self.assertIn("videoIO.unobserve(n)", unmount)
        self.assertIn("cards.delete(c.dataset.key);", unmount)

    def test_a_video_holds_a_player_only_near_the_viewport(self):
        """A <video> costs a media player from the moment it has a source, and
        creating or tearing one down was the 60-140 ms frame that survived the
        virtual grid. The card carries the URL; the observer attaches it."""
        video = block(SCRIPT, "if(it.fmt === 'video'){", "} else if(it.fmt === 'animated'){")
        self.assertIn("v.dataset.src = src", video)
        self.assertNotIn("v.src =", video, "a mounted card must not create a player by itself")
        attach = block(SCRIPT, "function attachVideo(v, on){", "function setPlaying")
        self.assertIn("v.src = v.dataset.src", attach)
        self.assertIn("v.removeAttribute('src'); v.load();", attach,
                      "removing the attribute alone leaves the player alive")
        self.assertIn("rootMargin: '0px'", block(SCRIPT, "const videoIO", "function attachVideo"))
        self.assertIn("v.poster=", video, "a still poster is visible before a decoder is attached")
        # Playing implies a source, whichever observer fired first.
        self.assertIn("if (on) attachVideo(v, true);", block(SCRIPT, "function setPlaying(v, on){", "}"))

    def test_the_sticky_header_does_not_blur_its_backdrop(self):
        """backdrop-filter re-blurs everything behind it on every scroll frame.

        It is the most expensive thing a sticky bar can do, and over an opaque
        background it buys nothing.
        """
        # Strip CSS comments first. Twice now a check like this has passed or
        # failed on the COMMENT explaining the rule rather than the rule.
        rule = re.sub(r"/\*.*?\*/", "", block(PAGE, "header{", "}"), flags=re.S)
        self.assertNotIn("backdrop-filter", rule)


class TheGridIsVirtual(unittest.TestCase):
    """Only the rows near the viewport exist. Measured on the 1 063-card
    catalog: every drag step re-laid-out 1 062 cards (24-46 ms per pointer
    move, 40 % of frames over 32 ms) and a scroll sweep spent 802 ms in long
    tasks. With the window it is 16-24 ms, none over 32 ms, and 113 ms.
    """

    def test_only_rows_near_the_viewport_are_in_the_document(self):
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertIn("const pad = innerHeight / 2;", render)
        self.assertIn("let first = rowAt(viewTop - pad);", render)
        # Two spacers carry the height of everything above and below, so the
        # scrollbar and the jump buttons see the whole grid.
        self.assertIn("setSpacer(padTop, first > 0 ? rows[first].top - G.gap : 0);", render)
        self.assertIn("setSpacer(padBot, bottom < total ? total - bottom - G.gap : 0);", render)
        for spacer in ('id="padTop"', 'id="padBot"'):
            self.assertIn(spacer, PAGE)
        self.assertIn(".spacer{grid-column:1/-1", PAGE)

    def test_the_layout_is_arithmetic_on_whole_pixels(self):
        """Every offset is a sum of row heights JavaScript chose, handed to CSS
        as variables and rounded to whole pixels; a fractional row height would
        drift the window by a pixel per row over a long grid. A card is a
        fixed height for the same reason: a row whose height depends on its
        longest label cannot be positioned without rendering it first."""
        layout = block(SCRIPT, "function layoutRows(){", "function measure")
        self.assertIn("Math.round(BASE.gap * zoom)", layout)
        self.assertIn("Math.round((compact ? BASE.cardHCompact : BASE.cardH) * zoom)", layout)
        self.assertIn("grid.style.setProperty('--cardH', G.cardH + 'px');", layout)
        card = block(PAGE, ".card{", "}")
        self.assertIn("height:var(--cardH", card)
        self.assertIn("overflow:hidden", card)
        # Line-anchored: `.card.logo .lbl{` comes first in the sheet and would match.
        self.assertIn("-webkit-line-clamp:2", block(PAGE, "\n.lbl{", "}"), "the label cannot decide the height")
        # The window IS the lazy loading; the old per-card hint has no job left.
        self.assertNotIn("content-visibility", PAGE)
        self.assertNotIn("contain-intrinsic-size", PAGE)

    def test_a_separator_starts_a_fresh_row_like_css_would(self):
        """Card rows are chunked exactly as CSS grid auto-placement would place
        them: a full-width marker takes its own row and the next card row
        starts under it. If the arithmetic and the browser ever disagreed, the
        spacers would be wrong by one row for every pack."""
        layout = block(SCRIPT, "function layoutRows(){", "function measure")
        self.assertIn("if(sepAt.has(i)){", layout)
        self.assertIn("indices.length < G.cols && !sepAt.has(visible[v])", layout)

    def test_scrolling_within_the_same_rows_costs_a_binary_search(self):
        """render() runs once per frame while scrolling. Between row
        transitions it must find the same window and stop; a full reconcile
        per frame measured 4 ms of JavaScript on every scroll frame."""
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertIn("if(first === win.first && last === win.last) return;", render)
        # But a changed model must always project, whatever the window was.
        layout = block(SCRIPT, "function layoutRows(){", "function measure")
        self.assertIn("win = {first: -1, last: -1};", layout)
        self.assertIn("requestAnimationFrame", block(SCRIPT, "function scheduleRender(){", "}"))

    def test_reconciling_moves_nodes_and_retires_only_the_stale_ones(self):
        render = block(SCRIPT, "function render(){", "function retire")
        self.assertIn("if(n === cur){ cur = cur.nextSibling; continue; }", render)
        self.assertIn("grid.insertBefore(n, cur);", render)
        self.assertIn("retire(stale)", render)


class ZoomFitsMoreOrLess(unittest.TestCase):
    """Zoom out to see more emoji per screen, in to inspect one."""

    def test_the_header_offers_zoom_in_out_and_reset(self):
        for btn in ('id="zoomOut"', 'id="zoomReset"', 'id="zoomIn"'):
            self.assertIn(btn, PAGE)
        self.assertIn("document.getElementById('zoomIn').onclick = ()=>setZoom(zoom * ZOOM_STEP);", SCRIPT)
        self.assertIn("document.getElementById('zoomOut').onclick = ()=>setZoom(zoom / ZOOM_STEP);", SCRIPT)
        # zoomReset is a typeable percentage (panel-holding.js), not a plain
        # reset button any more -- Enter applies it, double-click still resets.
        self.assertIn('<input id="zoomReset"', PAGE)
        self.assertIn("if(e.key==='Enter'){e.preventDefault();applyZoomInput();}", SCRIPT)
        self.assertIn("zoomInput.addEventListener('dblclick',()=>setZoom(1));", SCRIPT)

    def test_ctrl_wheel_and_ctrl_keys_are_taken_over_from_the_browser(self):
        """Ctrl+wheel is the browser's own page zoom. Left alone, both would
        fire: the grid re-zooms AND the whole page scales."""
        wheel = block(SCRIPT, "addEventListener('wheel'", "{passive: false});")
        self.assertIn("if(!e.ctrlKey) return;", wheel)
        self.assertIn("e.preventDefault();", wheel, "without it the browser zooms too")
        keys = block(SCRIPT, "addEventListener('keydown', e=>{", "});")
        for key in ("'='", "'-'", "'0'"):
            self.assertIn(key, keys)

    def test_the_level_is_clamped_and_remembered(self):
        """Persistence goes through the guarded adapter, never straight at
        localStorage: a browser that blocks site data THROWS on the property,
        and the unguarded read this replaced took the rest of the file with it.
        That the level really does survive a reload is asserted against a real
        browser in ``tests/test_panel_browser.py``."""
        z = block(SCRIPT, "function setZoom(z){", "function paintZoom")
        self.assertIn("Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))", z)
        self.assertIn("prefs.set('panelZoom'", z)
        self.assertIn("prefs.get('panelZoom'", block(SCRIPT, "function loadZoom(){", "}"))
        # Every preference, not just this one -- one raw call is the whole bug.
        raw = re.findall(r"localStorage\.(?:get|set)Item", SCRIPT)
        self.assertEqual(len(raw), 2, f"unguarded localStorage access: {raw}")
        self.assertIn("const prefs = {", SCRIPT)

    def test_zooming_keeps_the_top_item_on_screen(self):
        """Zooming is for seeing more or less of the same place, not for losing
        it: the item at the top of the screen is re-scrolled to the top."""
        z = block(SCRIPT, "function setZoom(z){", "function paintZoom")
        self.assertLess(z.index("const anchor = firstVisibleIndex();"), z.index("zoom = z;"))
        self.assertLess(z.index("layoutRows();"),
                        z.index("scrollTo(0, gridTop + rows[rowOfItem[anchor]].top - headerH)"))

    def test_far_out_the_text_rows_are_dropped(self):
        """Zoomed far out the text under a thumbnail is unreadable anyway;
        dropping it lets the rows pack tighter, which is what zooming out is
        for. The height model follows: a compact card has its own base height."""
        self.assertIn("const COMPACT_BELOW = 0.75;", SCRIPT)
        self.assertIn("document.body.classList.toggle('compact', compact);", SCRIPT)
        self.assertIn("body.compact .glyph,body.compact .lbl,body.compact .sub{display:none}", PAGE)

    def test_everything_in_a_card_scales_from_one_font_size(self):
        """One `font-size` on the card, everything inside in em: the zoom
        factor scales thumbnail, badges and label as one unit. A pixel size
        left inside would stay put while the card around it grew."""
        card = block(PAGE, ".card{", "}")
        self.assertIn("font-size:calc(12px*var(--z", card)
        self.assertIn("width:9em;height:9em", block(PAGE, ".thumb{", "}"))
        self.assertIn("width:8.67em;height:8.67em", block(PAGE, ".thumb img,.thumb video{", "}"))
        self.assertNotIn("104px", PAGE)
        self.assertNotIn("img.width = 104", SCRIPT, "an attribute size would pin the artwork at 100 %")


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
        self.assertTrue(PAGE.startswith("<!doctype html>"))
        self.assertTrue(PAGE.rstrip().endswith("</html>"))
        # Every placeholder the handler substitutes must survive extraction --
        # a page missing one renders the literal token to the browser.
        for token in ("__ITEMS__", "__TOKEN__", "__PREVIEW_FPS__",
                      "__PER_SET__", "__HIDDEN__", "__ASSET_VER__"):
            self.assertIn(token, PAGE, f"{token} lost in the asset")


class SelectionModeCarriesARun(unittest.TestCase):
    """Picking several emoji and moving them in one gesture.

    Arranging a 200-card pack one card at a time is the slow part. The risk it
    introduces is the reason these are pinned: a selection that silently changes
    what SHIPS, or a cancelled group drag that leaves half the run somewhere new.
    """

    def test_the_pick_box_is_not_the_tick(self):
        """They answer different questions -- "does this ship" and "does this
        move with the others". One control for both would let arranging drop an
        emoji from the pack by accident."""
        self.assertIn("el('span','pick'", SCRIPT)
        self.assertIn("el('span','tick'", SCRIPT)
        # Invisible until the mode is on, so the card is unchanged until asked.
        self.assertIn("display:none", block(PAGE, ".pick{", "}"))
        self.assertIn("body.selmode .pick{display:flex}", PAGE)

    def test_only_a_picked_box_draws_a_check(self):
        """It used to be written into the element, so both states rendered the
        same glyph and only the colour changed: an unselected box looked
        checked, and two states were told apart by colour alone. The glyph is
        the signal now, and it comes from one place."""
        for built in ("el('span','pick')", "el('span','pick');"):
            if built in SCRIPT:
                break
        else:
            self.fail("the pick box must be built with no glyph of its own")
        self.assertNotIn("el('span','pick','✓')", SCRIPT)
        self.assertNotIn("el('span','pick', '✓')", SCRIPT)
        rule = ".card.picked .pick::after,.hcard.picked .pick::after"
        self.assertIn(rule, PAGE, "the check must be drawn for the picked state")
        self.assertIn("✓", block(PAGE, rule + "{", "}"))

    def test_the_empty_box_is_still_visible_over_artwork(self):
        """An empty box with no fill would vanish over a light thumbnail, and
        the state would then be unreadable in the other direction."""
        base = block(PAGE, ".pick{", "}")
        self.assertIn("border:", base)
        self.assertIn("background:#0b1422", base)

    def test_leaving_the_mode_drops_the_picks(self):
        """A hidden selection that still moves cards on the next drag is worse
        than no selection at all."""
        self.assertIn("if(!selMode) clearPicked();", SCRIPT)

    def test_arranging_never_changes_what_ships(self):
        """The card click toggles `included`. In selection mode that click now
        PICKS instead -- it used to do nothing at all, which left the 1.7em box
        as the only way to select on a 140px card -- and either way a click
        while arranging must never quietly drop an emoji from the pack."""
        body = block(SCRIPT, "grid.addEventListener('click'", "// --- Losing the server")
        self.assertIn("if(selMode){", body)
        self.assertIn("pickCardAt(i, e.shiftKey);", body)
        self.assertLess(body.index("if(selMode){"), body.index("remember();"),
                        "the branch has to come before anything mutates inclusion")

    def test_the_box_owns_its_own_click(self):
        """Its `pointerdown` has already toggled. Acting on the click as well
        would toggle twice and net to zero -- the same double-toggle that made
        the tray's fix a replacement rather than an addition."""
        body = block(SCRIPT, "grid.addEventListener('click'", "// --- Losing the server")
        self.assertIn("if(!e.target.closest('.pick')) pickCardAt", body)

    def test_a_picked_card_is_marked_by_a_moving_ring_on_both_surfaces(self):
        """A flat inset outline among four format accents is what the owner
        could not find. The ring is the same control in the grid and the tray:
        two markers for one state would be a worse outcome than the bug."""
        rule = ".card.picked::before,.hcard.picked::before"
        self.assertIn(rule, PAGE)
        ring = block(PAGE, rule + "{", "}")
        self.assertIn("conic-gradient", ring)
        self.assertIn("animation:pickspin", ring)
        self.assertIn("@keyframes pickspin{to{transform:rotate(1turn)}}", PAGE,
                      "the rotation must be a transform: the compositor animates "
                      "that without repainting, and this grid is virtual because "
                      "repainting cost 14-17 ms per pointer move")
        self.assertNotIn(".card.picked{outline:2px solid #f0abfc;outline-offset:-2px}", PAGE,
                         "the flat outline it replaces must be gone, not doubled")

    def test_reduced_motion_keeps_the_ring_and_stops_it_moving(self):
        """The preference is about movement, not about the marker."""
        reduced = block(PAGE, "@media (prefers-reduced-motion:reduce){", "}}")
        self.assertIn(".card.picked::before,.hcard.picked::before{animation:none", reduced)

    def test_dragging_a_picked_card_carries_the_whole_set_even_off_screen(self):
        """The carried set is read from ITEMS, not from the DOM: a picked card
        whose row is not mounted must still travel, or the run splits in two."""
        body = block(SCRIPT, "grid.addEventListener('dragstart'", "grid.addEventListener('dragover'")
        self.assertIn("picked.has(dragKey)", body)
        self.assertIn("ITEMS.filter(x=>picked.has(x.key) && !x.isLogo)", body)
        self.assertNotIn("querySelectorAll('.card.picked')", body)
        # An unpicked card is still the single-card gesture that always worked.
        self.assertIn("[dragKey]", body)

    def test_a_pick_survives_the_card_being_unmounted(self):
        """Picks live in a Set keyed by content key; the card only PAINTS it,
        and a card built later for the same key paints it again."""
        self.assertIn("if(node) node.classList.toggle('picked', on);", SCRIPT)
        self.assertIn("c.classList.toggle('picked', picked.has(it.key));",
                      block(SCRIPT, "function mount(k){", "function unmountCard"))

    def test_dragging_back_shrinks_the_run(self):
        """Overshooting has to be correctable inside the same stroke; without a
        baseline the run only ever grows and the mode has to be left to fix it."""
        body = block(SCRIPT, "function applyStroke(", chr(10) + "}")
        self.assertIn("strokeBase.has(", body)
        self.assertIn("if(!now.has(k))", body)


class PackSplitsAndJumpButtons(unittest.TestCase):
    """The pack-boundary markers, and the two jump buttons beside them."""

    def test_a_separator_is_never_a_card(self):
        """The drop handler resolves its target with closest('.card').

        A separator carrying that class would sit between cards, swallow a drop
        aimed past it and do nothing -- the same shape as the bug where a drop
        on a grid gap silently threw the emoji to the end. It is `.packsep`.
        """
        self.assertIn("grid-column:1/-1", block(PAGE, ".packsep{", "}"))
        body = block(SCRIPT, "function sepNode(", chr(10) + "function ")
        self.assertIn("el('div','packsep')", body)
        self.assertNotIn("'card'", body)
        self.assertNotIn("packsep card", SCRIPT)
        # And the drop handler still keys off .card, so the two cannot meet.
        self.assertIn("e.target.closest('.card')", SCRIPT)

    def test_the_splits_are_counted_from_included_items_only(self):
        """An unticked card never ships, so it cannot push the boundary."""
        body = block(SCRIPT, "function packStarts(){", "function layoutRows")
        self.assertIn("!it.isLogo && it.included", body)
        # capacity leaves a slot for the logo, exactly as build_collection does.
        self.assertIn("PER_SET - (logo ? 1 : 0)", body)

    def test_a_candidate_no_longer_splits_the_pack_it_was_dropped_into(self):
        """Dragging one emoji into a pack used to open a "Not in a pack yet"
        run: a full-width marker plus the empty rest of its row, for every
        single card moved. The owner arranges by dropping candidates into a
        pack, so that made the grid unusable exactly when it was being used.
        """
        self.assertNotIn("'Not in a pack yet'", SCRIPT)
        body = block(SCRIPT, "function packStarts(){", "function layoutRows")
        # A null pack leaves `cur` alone, so the run it was dropped into
        # continues -- which is also the pack it will publish into.
        self.assertIn("pk !== null && (!starts.length || pk !== cur)", body)

    def test_the_logo_opens_its_own_packs_run(self):
        """The logo IS emoji 0 of its pack. While it was excluded from run
        detection the marker landed one card below it, so pack 5's logo drew
        above pack 5's header and read as part of the pack before it."""
        body = block(SCRIPT, "function packStarts(){", "function layoutRows")
        i_mem = body.index("if(byMembership){")
        i_cap = body.index("} else if(!it.isLogo && it.included){")
        self.assertLess(i_mem, i_cap, "membership mode must not filter logos out")
        self.assertNotIn("isLogo", body[i_mem:i_cap], "a logo has to be able to start its pack's run")

    def test_selection_changes_recompute_the_splits(self):
        """Both inclusion paths must relayout, not just update the counter.

        Unticking enough cards genuinely moves a boundary, so a counter-only
        refresh left the markers lying.
        """
        # `markSelDirty()` rides along on both paths now -- a selection change
        # is also the moment the page learns it differs from what the server
        # acknowledged -- so this asserts the relayout, not the exact tail.
        self.assertIn("relayout(); updateCount(); markSelDirty(); }", SCRIPT)
        self.assertIn("lastIdx=i; relayout(); updateCount(); markSelDirty();",
                      SCRIPT)

    def test_separators_are_part_of_the_row_model_not_accumulated(self):
        """Rebuilt from the pack starts on every layout and reused by title, so
        they can neither pile up nor go stale."""
        layout = block(SCRIPT, "function layoutRows(){", "function measure")
        self.assertIn("const sepAt = new Map();", layout)
        self.assertIn("rows.push({sep: sepAt.get(i), at: i, top: y, h: G.sepH});", layout)

    def test_one_pack_needs_no_divider(self):
        self.assertIn("if(starts.length >= 2){", block(SCRIPT, "function layoutRows(){", "function measure"))

    def test_the_header_offers_top_and_bottom(self):
        self.assertIn('id="top"', PAGE)
        self.assertIn('id="bot"', PAGE)
        # Document scrolling, NOT scrollIntoView: that aligns with the top of
        # the viewport, which sits behind the sticky header, so Top stopped one
        # header short of the Pack 1 marker and Bottom stopped short too. The
        # bottom spacer makes scrollHeight exact, so Bottom really is the end.
        jump = SCRIPT[SCRIPT.index("document.getElementById('top').onclick"):][:400]
        self.assertIn("window.scrollTo({top:0})", jump)
        self.assertIn("document.documentElement.scrollHeight", jump)
        self.assertNotIn("scrollIntoView", jump)


if __name__ == "__main__":
    unittest.main()
