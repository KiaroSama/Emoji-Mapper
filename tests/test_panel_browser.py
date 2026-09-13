"""What the panel's client code actually DOES, in a real browser.

The other panel modules assert against the text the server sends. That is how
seven audited defects survived review: a save pipeline that forgets the newest
edit, a separator cache that hands two runs one node, and a zoom that clamps
against the old document height are all perfectly well-formed JavaScript, and a
source-text assertion cannot tell any of them from a correct one. So this
module serves the real page from a real catalog, drives the real controls, and
asserts the result -- including the order the catalog is left holding.

Needs playwright and its Chromium build (``requirements-dev.txt``). Missing
either is a hard error, never a skip: a browser test that reports green on a
machine with no browser is worse than no browser test at all. Set
``EMOJI_MAPPER_NO_BROWSER_TESTS=1`` to opt out deliberately.
"""

from __future__ import annotations

import json
import re
import sys
import time
import unittest

from tests import _panel_browser_fixtures as fx
from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

import panel
from tests._panel_browser_fixtures import DENY_STORAGE, synth

# Eight, because the drag and revision suites below reach for card six. One
# catalog for the module rather than one per class: the tests that care about
# real rows read the order back out of it, and the rest serve a synthetic model.
H = fx.Harness(catalog_size=8)


def setUpModule():
    H.start()


def tearDownModule():
    H.stop()


class PanelInABrowser(unittest.TestCase):
    """A real panel, served from a temp catalog, driven in headless Chromium.

    Headless on purpose: an occluded headed window is throttled to 1-2 rAF a
    second, which turns every timing assertion here into a coin toss.

    The harness itself lives in `_panel_browser_fixtures` so the queue suite can
    drive the same page without importing this module -- importing a module that
    owns `test_*` methods collects those tests a second time rather than sharing
    anything.
    """

    def setUp(self):
        self.errors: list[str] = []

    def open(self, items=None, **kw):
        page = H.open(items, on_error=self.errors.append,
                      cleanup=self.addCleanup, **kw)
        return page

    def db_order(self):
        return H.db_order()

    def assertNoPageErrors(self, page):
        self.assertEqual(self.errors, [], "the page threw during setup")



class F01OrderSavesAreRevisioned(PanelInABrowser):
    """An acknowledgement may only speak for the arrangement it acknowledges.

    Keep one save in flight and queue a newer one: the old reply arriving first
    used to clear the newer arrangement on success and overwrite it on failure,
    and in both cases the browser stopped asking before the tab was closed.
    """

    CATALOG = 8

    def test_F01_an_old_acknowledgement_does_not_clear_a_newer_arrangement(self):
        page = self.open(clock=True)
        page.evaluate("__reorder(0, 5)")
        page.clock.run_for(500)                       # the 400 ms debounce
        self.assertEqual(len(page.evaluate("__sent('/api/order')")), 1)

        page.evaluate("__reorder(1, 7)")              # a newer arrangement
        newer = page.evaluate("pendingOrder")
        page.evaluate("__settle(0, 200)")             # the OLD save comes back ✓
        page.clock.run_for(50)

        self.assertEqual(page.evaluate("pendingOrder"), newer,
                         "an older acknowledgement cleared newer unsaved work")
        self.assertTrue(page.evaluate("__unload()"),
                        "the tab was free to close on an arrangement never saved")
        page.clock.run_for(2000)
        sent = page.evaluate("__sent('/api/order')")
        self.assertEqual(len(sent), 2, "the newer arrangement was never sent")
        self.assertEqual(sent[1]["body"]["order"], newer)

        page.evaluate("__settle(1, 200)")
        page.clock.run_for(50)
        self.assertIsNone(page.evaluate("pendingOrder"))
        self.assertFalse(page.evaluate("__unload()"), "nothing is unsaved now")

    def test_F01_a_failed_save_does_not_overwrite_a_newer_arrangement(self):
        page = self.open(clock=True)
        page.evaluate("__reorder(0, 5)")
        page.clock.run_for(500)
        stale = page.evaluate("__sent('/api/order')")[0]["body"]["order"]

        page.evaluate("__reorder(1, 7)")
        newer = page.evaluate("pendingOrder")
        self.assertNotEqual(stale, newer)
        page.evaluate("__settle(0, 500)")             # the OLD save failed
        page.clock.run_for(50)

        self.assertEqual(page.evaluate("pendingOrder"), newer,
                         "a failed old snapshot replaced the newer arrangement")
        page.clock.run_for(3000)                      # bounded backoff
        sent = page.evaluate("__sent('/api/order')")
        self.assertEqual(sent[-1]["body"]["order"], newer,
                         "the retry pushed the stale arrangement")

    def test_F01_only_one_order_request_is_ever_in_flight(self):
        """The heartbeat and the debounce both flush, and they overlap."""
        page = self.open(clock=True)
        page.evaluate("__reorder(0, 5)")
        page.evaluate("(()=>{ flushOrder(pendingOrder); flushOrder(pendingOrder); })()")
        self.assertEqual(len(page.evaluate("__sent('/api/order')")), 1,
                         "two concurrent saves race for the same catalog rows")

        page.evaluate("__reorder(1, 7)")              # an edit during the save
        page.evaluate("(()=>{ flushOrder(pendingOrder); })()")
        page.clock.run_for(1000)                      # ...and the debounce fires
        self.assertEqual(len(page.evaluate("__sent('/api/order')")), 1)

        page.evaluate("__settle(0, 200)")
        page.clock.run_for(50)
        sent = page.evaluate("__sent('/api/order')")
        self.assertEqual(len(sent), 2, "the queued arrangement never followed")
        self.assertEqual(sent[1]["body"]["order"], page.evaluate("pendingOrder"))


class F01TheCatalogKeepsTheNewestOrder(PanelInABrowser):
    """The JavaScript variable is not the evidence; the catalog is."""

    CATALOG = 8

    def test_F01_a_failed_save_still_lands_the_newest_arrangement(self):
        page = self.open(clock=True)
        posted = []
        page.on("request", lambda r: posted.append(r.post_data)
                if "/api/order" in r.url else None)

        page.evaluate("__reorder(0, 5)")
        page.clock.run_for(500)                       # save A, gated
        stale = page.evaluate("__sent('/api/order')")[0]["body"]["order"]
        page.evaluate("__reorder(1, 7)")              # a newer arrangement
        newer = page.evaluate("pendingOrder")
        page.evaluate("__net.passthrough = true")     # the panel is back
        page.evaluate("__settle(0, 500)")             # ...and A had failed

        deadline = time.monotonic() + 20
        order = self.db_order()
        while order != newer and time.monotonic() < deadline:
            page.clock.run_for(1100)                  # the page's own backoff
            time.sleep(0.1)
            order = self.db_order()
        self.assertEqual(order, newer,
                         "the catalog kept an arrangement the owner had replaced")
        self.assertNotIn(stale, [self._order_of(b) for b in posted],
                         "a stale snapshot was pushed at the catalog")

        page.reload(wait_until="load")
        self.assertEqual(page.evaluate("ITEMS.filter(x=>!x.isLogo).map(x=>x.key)"),
                         newer, "the reloaded page disagrees with the catalog")

    def test_F01_a_real_drag_reaches_the_catalog(self):
        """The same pipeline through the gesture that actually uses it."""
        page = self.open()
        page.evaluate("__net.passthrough = true")
        before = self.db_order()
        page.locator("#grid .card").nth(0).drag_to(page.locator("#grid .card").nth(5))
        moved = page.evaluate("ITEMS.map(x=>x.key)")
        self.assertNotEqual(moved, before, "the drag did not move anything")

        deadline = time.monotonic() + 20
        order = self.db_order()
        while order != moved and time.monotonic() < deadline:
            time.sleep(0.1)
            order = self.db_order()
        self.assertEqual(order, moved)

    @staticmethod
    def _order_of(body):
        return json.loads(body)["order"] if body else None


class F02SelectionSavesSurviveAFailure(PanelInABrowser):
    """A ping proves the process is alive, never that a save landed.

    A failed Save left nothing behind: the next successful ping took the
    warning down, no retry was ever made, and the tab closed without asking.
    """

    def _failed_save(self, page):
        """Deselect one card, press Save, and have the panel refuse it."""
        page.locator("#grid .card").nth(1).click()
        page.click("#save")
        saves = page.evaluate("__sent('/api/save')")
        self.assertEqual(len(saves), 1, "Save did not reach the panel")
        page.evaluate(f"__settle({saves[0]['i']}, 500)")
        page.clock.run_for(50)
        self.assertTrue(self.alerting(page), "a refused Save must not be silent")
        return saves[0]["body"]["excluded"]

    @staticmethod
    def alerting(page):
        return page.evaluate(
            "document.getElementById('alert').classList.contains('show')")

    def test_F02_a_successful_ping_does_not_dismiss_a_failed_save(self):
        page = self.open(clock=True)
        self._failed_save(page)
        page.clock.run_for(6000)                      # the 5 s heartbeat pings ✓
        self.assertTrue(self.alerting(page),
                        "liveness was reported as persistence")

    def test_F02_the_tab_is_protected_until_the_save_lands(self):
        page = self.open(clock=True)
        self._failed_save(page)
        self.assertTrue(page.evaluate("__unload()"),
                        "the tab could close on a selection that never saved")
        page.clock.run_for(6000)                      # the retry goes out
        pending = [c for c in page.evaluate("__sent('/api/save')") if not c["settled"]]
        self.assertEqual(len(pending), 1, "exactly one retry, and it is in flight")
        page.evaluate(f"__settle({pending[0]['i']}, 200)")
        page.clock.run_for(50)
        self.assertFalse(self.alerting(page), "the warning outlived the problem")
        self.assertFalse(page.evaluate("__unload()"))

    def test_F02_a_refused_save_is_retried_with_what_Save_asked_for(self):
        """Selection persists only when Save is pressed, so the retry carries
        the snapshot Save was given -- not edits made since, which the owner
        has not asked to keep."""
        page = self.open(clock=True)
        asked = self._failed_save(page)
        page.locator("#grid .card").nth(2).click()    # a newer, unrequested edit
        page.clock.run_for(6000)
        saves = page.evaluate("__sent('/api/save')")
        self.assertGreater(len(saves), 1, "a refused Save was simply forgotten")
        self.assertEqual(saves[-1]["body"]["excluded"], asked,
                         "the retry saved something Save was never asked to save")

    def test_F02_an_order_success_does_not_dismiss_a_failed_save(self):
        page = self.open(clock=True)
        self._failed_save(page)
        page.evaluate("__reorder(0, 2)")
        page.clock.run_for(500)
        order = page.evaluate("__sent('/api/order')")[-1]
        page.evaluate(f"__settle({order['i']}, 200)")
        page.clock.run_for(50)
        self.assertTrue(self.alerting(page),
                        "a saved ORDER cleared a failed SELECTION's warning")


class F03EveryRunHasItsOwnSeparator(PanelInABrowser):
    """Live membership can revisit a pack: 1, 2, 1 is three runs, not two.

    The marker cache was keyed by the label, so both "Pack 1" runs were handed
    the same node -- and one node cannot be in two places, so the grid drew two
    boundaries where the row model had counted three.
    """

    RUNS = [1, 1, 2, 2, 1, 1]

    def test_F03_a_repeated_pack_gets_a_separator_of_its_own(self):
        page = self.open(synth(6, packs=self.RUNS))
        self.assertEqual(page.evaluate("rows.filter(r=>r.sep).length"), 3,
                         "the fixture must model three runs")
        self.assertEqual(
            page.evaluate("document.querySelectorAll('#grid .packsep').length"), 3,
            "two runs shared one marker, so the grid lost a boundary")

    def test_F03_each_marker_sits_at_the_head_of_its_own_run(self):
        page = self.open(synth(6, packs=self.RUNS))
        laid_out = page.evaluate("""
            [...grid.children]
              .filter(n => n.classList.contains('card') || n.classList.contains('packsep'))
              .map(n => n.classList.contains('packsep')
                        ? n.querySelector('span').textContent
                        : '#' + n.querySelector('.pos').textContent)""")
        self.assertEqual(laid_out, ["Pack 1", "#1", "#2",
                                    "Pack 2", "#3", "#4",
                                    "Pack 1", "#5", "#6"])

    def test_F03_reordering_keeps_one_marker_per_run(self):
        page = self.open(synth(6, packs=self.RUNS))
        page.evaluate("__reorder(5, 0)")              # 1,1,1,2,2,1 -- still 3 runs
        self.assertEqual(
            page.evaluate("rows.filter(r=>r.sep).length"),
            page.evaluate("document.querySelectorAll('#grid .packsep').length"),
            "the row heights and the markers disagree after a move")

    def test_F03_a_run_that_goes_away_takes_its_marker_with_it(self):
        page = self.open(synth(6, packs=self.RUNS))
        page.evaluate("""(()=>{ for(const i of [2,3]){ ITEMS[i].included = false;
                                setCard(ITEMS[i]); } relayout(); updateCount(); })()""")
        self.assertEqual(
            page.evaluate("document.querySelectorAll('#grid .packsep').length"),
            page.evaluate("rows.filter(r=>r.sep).length"),
            "a marker for a run that no longer exists stayed in the grid")


class F04ZoomKeepsItsAnchor(PanelInABrowser):
    """Zooming is for seeing more or less of the same place.

    The scroll that restores the anchor used to run BEFORE the render that
    gives the document its new height, so the browser clamped it against the
    old extent: zooming in at the bottom of a thousand cards moved the top item
    from 980 to 784.
    """

    def _anchored(self, page, before):
        """The item that was at the top is still at the top.

        Zoom changes the column count, so the anchor cannot always be the FIRST
        item of the new top row -- it shares that row with whatever now precedes
        it. Same columns is the strict case; a changed count still has to leave
        the anchor in the top row, which is 8 cards, not the 200 this lost.
        """
        after = page.evaluate("__anchor()")
        if after["atEnd"] and after["top"]["start"] <= before["idx"]:
            return                                    # honest end-of-document clamp
        self.assertIsNotNone(after["top"], "the grid scrolled past its own rows")
        if after["cols"] == before["cols"]:
            self.assertEqual(after["top"]["start"], before["idx"],
                             "the zoom landed somewhere else in the grid")
        else:
            self.assertTrue(
                after["top"]["start"] <= before["idx"] < after["top"]["end"],
                f"the anchor {before['idx']} fell out of the top row {after['top']}")

    def test_F04_the_anchor_survives_a_zoom_anywhere_in_the_grid(self):
        page = self.open(synth(1000))
        places = {"top": "scrollTo(0, 0)",
                  "middle": "scrollTo(0, document.documentElement.scrollHeight / 2)",
                  "bottom": "scrollTo(0, document.documentElement.scrollHeight)"}
        for where, scroll in places.items():
            for button in ("#zoomIn", "#zoomOut"):
                with self.subTest(where=where, button=button):
                    page.evaluate("setZoom(1)")
                    page.evaluate(scroll)
                    before = page.evaluate("__anchor()")
                    self.assertGreaterEqual(before["idx"], 0)
                    page.click(button)
                    self._anchored(page, before)

    def test_the_zoom_level_survives_a_reload(self):
        """What `test_the_level_is_clamped_and_remembered` used to assert by
        looking for a localStorage call, asserted by reloading the page."""
        page = self.open(synth(40))
        page.click("#zoomIn")
        zoomed = page.evaluate("zoom")
        self.assertGreater(zoomed, 1)
        page.reload(wait_until="load")
        self.assertEqual(page.evaluate("zoom"), zoomed)
        # .value, not .textContent -- zoomReset is a typeable <input> now.
        self.assertEqual(page.evaluate("document.getElementById('zoomReset').value"),
                         f"{round(zoomed * 100)}%")

    def test_F04_a_zoom_round_trip_comes_back_to_the_same_item(self):
        page = self.open(synth(1000, packs=[1 + i // 150 for i in range(1000)]))
        page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
        before = page.evaluate("__anchor()")
        for _ in range(3):
            page.click("#zoomIn")
        # A single click now places the caret to type a value; double-click
        # is the reset gesture (zoomReset is a typeable <input>).
        page.dblclick("#zoomReset")
        self.assertEqual(page.evaluate("zoom"), 1)
        self._anchored(page, before)


class F05AFreezeIsNeverUndone(PanelInABrowser):
    """One predicate answers "may this animate", and a late callback obeys it.

    The observer decided from intersection, the master switch and reduced
    motion alone, so a callback delivered after a freeze restarted exactly what
    the freeze had stopped.
    """

    CARDS = 300

    def _barrier(self, page, action):
        """Run `action`, then wait until the browser has delivered the
        intersection change it caused.

        A probe observer registered after the page's own gets its callback
        after it in the same delivery, so its tick proves the page's observer
        has already had its chance to undo the freeze.
        """
        page.evaluate("""(()=>{
            window.__tick = 0;
            window.__probe = new IntersectionObserver(() => { window.__tick++; },
                                                      {root: null, rootMargin: '0px'});
            document.querySelectorAll('#grid img[data-anim]').forEach(n => __probe.observe(n));
        })()""")
        page.wait_for_function("__tick > 0", timeout=10_000)
        page.evaluate(f"(()=>{{ window.__tick = 0; {action} }})()")
        page.wait_for_function("__tick > 0", timeout=10_000)

    def test_F05_what_you_can_see_still_animates(self):
        page = self.open(synth(self.CARDS, fmt="animated"))
        page.wait_for_function("__animating() > 0", timeout=10_000)
        self.assertNoPageErrors(page)

    def test_F05_a_hidden_page_stays_frozen_when_new_cards_arrive(self):
        page = self.open(synth(self.CARDS, fmt="animated"))
        page.wait_for_function("__animating() > 0", timeout=10_000)
        page.evaluate("""(()=>{
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
            document.dispatchEvent(new Event('visibilitychange'));
        })()""")
        self.assertEqual(page.evaluate("__animating()"), 0, "the freeze did not happen")
        self._barrier(page, "scrollBy(0, innerHeight * 3);")
        self.assertEqual(page.evaluate("__animating()"), 0,
                         "an observer callback restarted a hidden page")
        self.assertNoPageErrors(page)

    def test_F05_a_switched_off_grid_stays_frozen_when_new_cards_arrive(self):
        page = self.open(synth(self.CARDS, fmt="animated"))
        page.click("#anim")
        self.assertEqual(page.evaluate("__animating()"), 0)
        self._barrier(page, "scrollBy(0, innerHeight * 3);")
        self.assertEqual(page.evaluate("__animating()"), 0,
                         "an observer callback overrode the master switch")

    def test_F05_a_scrolling_grid_stays_frozen_until_it_settles(self):
        page = self.open(synth(self.CARDS, fmt="animated"))
        page.wait_for_function("__animating() > 0", timeout=10_000)
        self._barrier(page, "__keepScrolling(4000);")
        state = page.evaluate("({thawing: scrollThaw !== null, playing: __animating()})")
        self.assertTrue(state["thawing"],
                        "the freeze window closed before the observer fired; "
                        "this machine is too slow for this assertion to mean anything")
        self.assertEqual(state["playing"], 0,
                         "an observer callback thawed a grid that was still scrolling")
        page.wait_for_function("__animating() > 0", timeout=10_000)   # and it thaws

    def test_F05_hover_under_reduced_motion_obeys_the_master_switch(self):
        """Hover is the reduced-motion exception, not a way round the switch."""
        page = self.open(synth(12, fmt="video"), reduced_motion="reduce")
        page.click("#anim")                           # animation off
        page.locator("#grid .thumb").nth(0).hover()
        self.assertEqual(page.evaluate("__plays"), 0,
                         "hover played a video the owner had switched off")
        page.click("#anim")                           # back on
        page.locator("#grid .thumb").nth(1).hover()
        self.assertGreater(page.evaluate("__plays"), 0,
                           "hover is the only way to play a video under reduced motion")


class F06ThePackCountMatchesTheGrid(PanelInABrowser):
    """Every pack is led by the logo, so every pack holds one fewer emoji.

    Dividing a total that contains ONE logo reported two packs for a selection
    the grid itself split into three.
    """

    def _packs(self, page):
        runs = max(1, page.evaluate("rows.filter(r=>r.sep).length"))
        warn = page.evaluate("document.getElementById('capWarn').textContent")
        shown = page.evaluate(
            "getComputedStyle(document.getElementById('capWarn')).display") != "none"
        said = re.search(r"(\d+) packs", warn)
        return runs, (int(said.group(1)) if said and shown else 1), warn

    def test_F06_the_count_is_the_number_of_runs_the_grid_draws(self):
        per = panel.PER_SET
        for ordinary in (per - 2, per - 1, per, 2 * per - 3, 2 * per - 2, 2 * per - 1):
            with self.subTest(ordinary=ordinary):
                page = self.open(synth(ordinary, logo=True))
                runs, said, warn = self._packs(page)
                self.assertEqual(said, runs,
                                 f"the grid drew {runs} packs, the header said "
                                 f"{said}: {warn!r}")

    def test_F06_without_a_logo_the_whole_set_is_available(self):
        page = self.open(synth(panel.PER_SET))
        runs, said, _warn = self._packs(page)
        self.assertEqual((runs, said), (1, 1), "a logo that is not there took a slot")

    def test_F06_an_excluded_card_takes_no_slot(self):
        per = panel.PER_SET
        page = self.open(synth(per, logo=True, excluded=range(3)))
        runs, said, _warn = self._packs(page)
        self.assertEqual((runs, said), (1, 1), "unticked cards were counted")

    def test_F06_live_membership_is_reported_as_an_estimate(self):
        """The page cannot see how full a published pack already is, and the
        publisher can be asked for mixed or per-format packs. Say so rather
        than print a number that is only sometimes right."""
        page = self.open(synth(6, packs=[1, 1, 2, 2, 3, 3]))
        _runs, _said, warn = self._packs(page)
        self.assertIn("estimate", warn.lower(), f"a guess presented as fact: {warn!r}")


class F07DeniedStorageStillBoots(PanelInABrowser):
    """Preferences are a convenience; the panel is not.

    One unguarded ``localStorage`` read at module scope threw, took the rest of
    the file with it and left ANIM_ON in its temporal dead zone -- so some cards
    mounted and nothing else in the page worked.
    """

    def test_F07_denied_site_data_does_not_abort_setup(self):
        page = self.open(init=DENY_STORAGE)
        self.assertNoPageErrors(page)
        self.assertEqual(page.evaluate("document.getElementById('zoomReset').value"),
                         "100%")
        self.assertEqual(page.evaluate("document.getElementById('animLabel').textContent"),
                         "Animation: On")
        self.assertEqual(page.evaluate("document.getElementById('bg').textContent"),
                         "Backdrop: Checker")

        page.click("#zoomIn")
        self.assertGreater(page.evaluate("zoom"), 1, "zoom is dead without storage")
        page.click("#anim")
        self.assertEqual(page.evaluate("document.getElementById('animLabel').textContent"),
                         "Animation: Off")
        page.click("#bg")
        self.assertEqual(page.evaluate("document.getElementById('bg').textContent"),
                         "Backdrop: Light")

        before = page.evaluate("document.getElementById('selCount').textContent")
        page.locator("#grid .card").nth(0).click()
        self.assertNotEqual(
            page.evaluate("document.getElementById('selCount').textContent"), before)
        page.click("#undo")
        self.assertEqual(
            page.evaluate("document.getElementById('selCount').textContent"), before)
        self.assertNoPageErrors(page)


    def test_F07_a_malformed_stored_preference_is_ignored(self):
        page = self.open(init="""
            localStorage.setItem('panelZoom', 'banana');
            localStorage.setItem('emojiBg', 'wat');
            localStorage.setItem('animOn', 'maybe');
        """)
        self.assertNoPageErrors(page)
        self.assertEqual(page.evaluate("zoom"), 1)
        self.assertEqual(page.evaluate("document.getElementById('bg').textContent"),
                         "Backdrop: Checker")
        self.assertEqual(page.evaluate("document.getElementById('animLabel').textContent"),
                         "Animation: On")


if __name__ == "__main__":
    unittest.main()
