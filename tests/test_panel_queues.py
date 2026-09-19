"""R07/R08: what the panel still owes the server, and how hard it may ask.

**R07 -- a pending request is not dirty state.** `pendingSel` means "a Save is
still trying"; it was also read as "there is unsaved work". Exclude A and press
Save; while that is in flight, exclude B without pressing Save again. A's reply
correctly acknowledges only A -- and then cleared `pendingSel`, showed "Saved"
and dropped the `beforeunload` guard, while the visible two-item exclusion
differed from the saved one-item exclusion. Ordinary unsaved ticks were
unprotected for the same reason: they never entered a queue at all.

**R08 -- a refusal that cannot be fixed by asking again.** HTTP 400 was
described in its own comment as unrecoverable and then routed through
`failOrder()`, which schedules a retry; the 5-second heartbeat called
`flushOrder` directly, walking past the backoff entirely, so eight heartbeats
produced eight more identical POSTs. And a fetch that never resolved left
`orderFlight` claimed for the life of the page, so every newer revision queued
behind it stopped going, with no deadline to release it.

Driven in a real browser on a FAKE clock: the retry schedule is the subject, so
it has to be advanced deterministically rather than waited out.
"""

from __future__ import annotations

import unittest

from tests import _panel_browser_fixtures as fx

H = fx.Harness()


def setUpModule():
    H.start()


def tearDownModule():
    H.stop()


class PanelQueueCase(unittest.TestCase):
    def setUp(self):
        self.errors: list[str] = []

    def open(self, **kw):
        page = H.open(on_error=self.errors.append, cleanup=self.addCleanup, **kw)
        self.addCleanup(self.assertNoPageErrors, page)
        return page

    def assertNoPageErrors(self, page):
        self.assertEqual(self.errors, [], "the page threw")

    def sent(self, page, kind):
        return page.evaluate(f"window.__sent({kind!r})")


class RefusalsBelongToTheSubmittedBody(PanelQueueCase):
    def test_a_newer_order_outlives_an_older_permanent_refusal(self):
        page = self.open(clock=True)
        page.evaluate("__reorder(0, 3)")
        page.clock.run_for(450)
        page.evaluate("__reorder(0, 2)")
        newer = page.evaluate("pendingOrder")
        page.clock.run_for(450)  # the newer debounce expires while A still owns the flight
        page.evaluate("__settle(0, 400)")
        page.clock.run_for(1000)
        requests = self.sent(page, "/api/order")
        self.assertEqual(len(requests), 2, "old refusal blocked newer valid work")
        self.assertEqual(requests[1]["body"]["order"], newer)
        page.evaluate("__settle(1, 200)")
        page.clock.run_for(50)
        self.assertIsNone(page.evaluate("pendingOrder"))

    def test_new_bodies_survive_old_refusals_and_keep_transient_retries(self):
        for kind in ("order", "save"):
            for rejection in (400, 409):
                with self.subTest(queue=kind, status=rejection):
                    page = self.open(clock=True)
                    edit = "__reorder(0,3)" if kind == "order" else "__toggle(0);__save()"
                    newer = "__reorder(0,2)" if kind == "order" else "__toggle(1);__save()"
                    page.evaluate(edit)
                    page.clock.run_for(450)
                    page.evaluate(newer)
                    page.clock.run_for(450)
                    page.evaluate(f"__settle(0,{rejection})")
                    page.clock.run_for(500)
                    self.assertEqual(len(self.sent(page, f"/api/{kind}")), 2)
                    page.evaluate("__settle(1,503)")
                    page.clock.run_for(1100)
                    requests = self.sent(page, f"/api/{kind}")
                    self.assertEqual(len(requests), 3, "new work inherited the old stuck flag")
                    self.assertEqual(requests[1]["body"], requests[2]["body"])
                    page.evaluate("__settle(2,200)")
                    page.clock.run_for(50)
                    pending = "pendingOrder" if kind == "order" else "pendingSel"
                    self.assertIsNone(page.evaluate(pending))
                    page.close()


class UnsavedSelectionIsProtected(PanelQueueCase):
    """R07. The question is always "does what I see match what was stored"."""

    def test_an_edit_with_no_save_is_dirty_and_blocks_unload(self):
        page = self.open(items=fx.synth(6))
        self.assertFalse(page.evaluate("window.__dirty()"), "clean on load")
        page.evaluate("window.__toggle(1)")
        self.assertTrue(page.evaluate("window.__dirty()"))
        self.assertTrue(page.evaluate("window.__dirtyShown()"), "not shown to me")
        self.assertTrue(page.evaluate("window.__unload()"),
                        "the tab would have closed on an unsaved tick")

    def test_an_edit_made_DURING_a_save_survives_its_acknowledgement(self):
        """The reproduction. The reply is correct; what it cleared was not."""
        page = self.open(items=fx.synth(6))
        page.evaluate("window.__toggle(1)")          # exclude A
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        submitted = self.sent(page, "/api/save")[0]["body"]["excluded"]
        self.assertEqual(len(submitted), 1)

        page.evaluate("window.__toggle(2)")          # exclude B, no Save
        page.evaluate("window.__settle(0, 200)")     # A's reply lands
        page.wait_for_function("window.__sent('/api/save')[0].settled === true")

        self.assertEqual(len(page.evaluate("window.__excluded()")), 2)
        self.assertTrue(page.evaluate("window.__dirty()"),
                        "an older acknowledgement cleared newer unsaved work")
        self.assertTrue(page.evaluate("window.__unload()"))

    def test_saving_the_newer_state_finally_clears_it(self):
        page = self.open(items=fx.synth(6))
        page.evaluate("window.__toggle(1)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        page.evaluate("window.__toggle(2)")
        page.evaluate("window.__settle(0, 200)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 2")
        page.evaluate("window.__settle(1, 200)")
        page.wait_for_function("window.__dirty() === false")
        self.assertFalse(page.evaluate("window.__unload()"))

    def test_undoing_back_to_the_acknowledged_state_is_clean_again(self):
        page = self.open(items=fx.synth(6))
        page.evaluate("window.__toggle(1)")
        self.assertTrue(page.evaluate("window.__dirty()"))
        page.evaluate("window.__toggle(1)")          # back to where we started
        self.assertFalse(page.evaluate("window.__dirty()"))
        self.assertFalse(page.evaluate("window.__unload()"))

    def test_a_failed_save_leaves_it_dirty_and_a_retry_clears_it(self):
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__toggle(1)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        page.evaluate("window.__settle(0, 503)")     # transient
        page.wait_for_function("window.__sent('/api/save')[0].settled === true")
        self.assertTrue(page.evaluate("window.__dirty()"))

        page.clock.run_for(2000)                     # past the first backoff
        page.wait_for_function("window.__sent('/api/save').length === 2")
        page.evaluate("window.__settle(1, 200)")
        page.wait_for_function("window.__dirty() === false")

    def test_a_selection_really_reaches_the_catalog(self):
        """Through the actual server, and read back out of SQLite."""
        page = self.open()                            # the real 4-item catalog
        page.evaluate("window.__net.passthrough = true")
        page.evaluate("window.__toggle(1)")
        key = page.evaluate("ITEMS[1].key")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__dirty() === false", timeout=15_000)
        self.assertIn(key, H.excluded_in_db())


class APermanentRefusalIsNotRetried(PanelQueueCase):
    """R08, first half."""

    def test_a_400_on_the_order_queue_stops_resubmitting(self):
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__reorder(0, 3)")
        page.clock.run_for(500)                       # past the 400 ms debounce
        page.wait_for_function("window.__sent('/api/order').length === 1")
        page.evaluate("window.__settle(0, 400)")
        page.wait_for_function("window.__sent('/api/order')[0].settled === true")

        # Well past every backoff AND several heartbeats.
        page.clock.run_for(60_000)
        self.assertEqual(len(self.sent(page, "/api/order")), 1,
                         "a refusal retrying cannot fix was sent again")

    def test_a_409_on_the_save_queue_stops_resubmitting(self):
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__toggle(1)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        page.evaluate("window.__settle(0, 409)")
        page.wait_for_function("window.__sent('/api/save')[0].settled === true")
        page.clock.run_for(60_000)
        self.assertEqual(len(self.sent(page, "/api/save")), 1)

    def test_a_permanent_refusal_still_guards_the_work(self):
        """Stopping the retry must not quietly drop what was never saved."""
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__toggle(1)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        page.evaluate("window.__settle(0, 409)")
        page.clock.run_for(30_000)
        self.assertTrue(page.evaluate("window.__unload()"),
                        "the tab could close on work nothing will retry")

    def test_pressing_save_again_is_a_new_request_and_does_go(self):
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__toggle(1)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 1")
        page.evaluate("window.__settle(0, 409)")
        page.clock.run_for(30_000)
        page.evaluate("window.__toggle(2)")
        page.evaluate("window.__save()")
        page.wait_for_function("window.__sent('/api/save').length === 2")

    def test_a_transient_refusal_is_still_retried(self):
        """The fix must not turn every failure into a dead end."""
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__reorder(0, 3)")
        page.clock.run_for(500)
        page.wait_for_function("window.__sent('/api/order').length === 1")
        page.evaluate("window.__settle(0, 503)")
        page.clock.run_for(2000)
        page.wait_for_function("window.__sent('/api/order').length === 2")


class TheHeartbeatHonoursTheSchedule(PanelQueueCase):
    """R08, second half: one scheduling authority, not two."""

    def test_the_heartbeat_does_not_send_inside_a_pending_backoff(self):
        """Measured in a window SHORTER than the backoff it must respect.

        Every attempt is settled immediately here, so nothing is in flight and
        nothing can time out: the only thing that could produce another POST in
        that window is a caller ignoring the schedule -- which is exactly what
        calling `flushOrder` straight from the 5-second heartbeat did.
        """
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("window.__reorder(0, 3)")
        page.clock.run_for(500)
        page.wait_for_function("window.__sent('/api/order').length === 1")

        # Fail three times to grow the backoff: 1 s, 2 s, then 4 s.
        for i, wait in enumerate((1200, 2200)):
            page.evaluate(f"window.__settle({i}, 503)")
            page.clock.run_for(wait)
            page.wait_for_function(
                f"window.__sent('/api/order').length === {i + 2}")
        page.evaluate("window.__settle(2, 503)")     # backoff is now 4 s
        page.wait_for_function("window.__sent('/api/order')[2].settled === true")

        before = len(self.sent(page, "/api/order"))
        page.clock.run_for(3500)                     # < 4 s, but > one heartbeat
        self.assertEqual(len(self.sent(page, "/api/order")), before,
                         "something sent inside the backoff window")

        page.clock.run_for(1000)                     # now the backoff is up
        page.wait_for_function(
            f"window.__sent('/api/order').length === {before + 1}")


class AHangingRequestDoesNotWedgeTheQueue(PanelQueueCase):
    """R08, third half. The flight flag must be released by a deadline."""

    def test_a_never_resolving_order_post_is_bounded(self):
        page = self.open(items=fx.synth(8), clock=True)
        page.evaluate("window.__reorder(0, 3)")
        page.clock.run_for(500)
        page.wait_for_function("window.__sent('/api/order').length === 1")
        # Never settled. A newer arrangement is made behind it.
        page.evaluate("window.__reorder(1, 5)")
        page.clock.run_for(500)

        # Past the request deadline: the abort frees the flight and the newest
        # revision goes. Without a deadline this stayed at one POST forever.
        page.clock.run_for(30_000)
        page.wait_for_function("window.__sent('/api/order').length >= 2",
                               timeout=15_000)

    def test_the_newest_revision_is_the_one_that_goes(self):
        page = self.open(items=fx.synth(8), clock=True)
        page.evaluate("window.__reorder(0, 3)")
        page.clock.run_for(500)
        page.wait_for_function("window.__sent('/api/order').length === 1")
        page.evaluate("window.__reorder(1, 6)")
        page.clock.run_for(500)
        page.clock.run_for(30_000)
        page.wait_for_function("window.__sent('/api/order').length >= 2",
                               timeout=15_000)
        calls = self.sent(page, "/api/order")
        live = page.evaluate("ITEMS.filter(x=>!x.isLogo).map(x=>x.key)")
        self.assertEqual(calls[-1]["body"]["order"], live,
                         "the retry sent a stale arrangement")


if __name__ == "__main__":
    unittest.main()


class CompleteSaveSnapshots(PanelQueueCase):
    """Pack intent is part of the same immutable revision as inclusion."""

    def test_pack_only_edits_are_dirty_and_the_old_ack_does_not_clear_them(self):
        page = self.open(items=fx.synth(6, packs=[1, 1, 1, 2, 2, 2]), clock=True)
        page.evaluate("__save(); ITEMS[1].pack=2; markSelDirty(); __settle(0,200)")
        page.clock.run_for(20)
        self.assertTrue(page.evaluate("selDirty() && __unload() && __dirtyShown()"))
        page.evaluate("__save(); __settle(1,200)")
        page.clock.run_for(20)
        self.assertFalse(page.evaluate("selDirty() || __unload()"))

    def test_retry_does_not_submit_new_pack_intent_without_another_save(self):
        page = self.open(items=fx.synth(6, packs=[1, 1, 1, 2, 2, 2]), clock=True)
        page.evaluate("__save(); __settle(0,503)")
        page.clock.run_for(20)
        page.evaluate("ITEMS[1].pack=2; markSelDirty()")
        page.clock.run_for(1100)
        calls = self.sent(page, "/api/save")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["body"], calls[1]["body"])
        page.evaluate("__settle(1,200)")
        page.clock.run_for(20)
        self.assertTrue(page.evaluate("selDirty() && __unload()"))

    def test_new_pack_only_body_outlives_old_permanent_refusal(self):
        for status in (400, 409):
            with self.subTest(status=status):
                page = self.open(items=fx.synth(6, packs=[1, 1, 1, 2, 2, 2]), clock=True)
                page.evaluate(f"__save(); ITEMS[1].pack=2; __save(); __settle(0,{status})")
                page.clock.run_for(20)
                self.assertEqual(len(self.sent(page, "/api/save")), 2)
                page.evaluate("__settle(1,503)")
                page.clock.run_for(1100)
                calls = self.sent(page, "/api/save")
                self.assertEqual(calls[1]["body"], calls[2]["body"])
                page.evaluate("__settle(2,200)")
                page.clock.run_for(20)
                self.assertFalse(page.evaluate("selDirty() || __unload()"))

    def test_reset_restores_the_absence_of_a_pack_assignment(self):
        page = self.open(items=fx.synth(6), clock=True)
        page.evaluate("remember(); ITEMS[1].pack=2; markSelDirty(); document.getElementById('resetAll').click()")
        self.assertFalse(page.evaluate("Object.hasOwn(ITEMS[1], 'pack')"))
        self.assertFalse(page.evaluate("selDirty()"))
        page.evaluate("undo()")
        self.assertEqual(page.evaluate("ITEMS[1].pack"), 2)
        page.evaluate("redo()")
        self.assertFalse(page.evaluate("Object.hasOwn(ITEMS[1], 'pack')"))


class AcknowledgementsMustBeComplete(PanelQueueCase):
    def test_invalid_success_bodies_keep_both_queues_pending_and_retryable(self):
        for kind in ("order", "save"):
            for body in ("null", "[]", "{}", "{ok:false}", "{ok:true,count:-1}"):
                with self.subTest(queue=kind, body=body):
                    page = self.open(items=fx.synth(6), clock=True)
                    page.evaluate("__reorder(0,3)" if kind == "order" else "__toggle(1);__save()")
                    page.clock.run_for(450)
                    page.evaluate(f"__net.calls[0].res({{ok:true,status:200,json:()=>Promise.resolve({body})}})")
                    page.clock.run_for(20)
                    pending, flight = ("pendingOrder", "orderFlight") if kind == "order" else ("pendingSel", "selFlight")
                    self.assertTrue(page.evaluate(f"{pending}!==null && {flight}===0 && __unload()"))
                    page.clock.run_for(1100)
                    self.assertEqual(len(self.sent(page, f"/api/{kind}")), 2)
                    page.evaluate("__settle(1,200)")
                    page.clock.run_for(20)
                    self.assertIsNone(page.evaluate(pending))
                    self.assertEqual(page.evaluate("__rejections"), [])

    def test_hanging_success_body_and_json_parse_error_do_not_acknowledge(self):
        for kind in ("order", "save"):
            for outcome in ("new Promise(()=>{})", "Promise.reject(new SyntaxError('truncated'))"):
                with self.subTest(queue=kind, outcome=outcome):
                    page = self.open(items=fx.synth(6), clock=True)
                    page.evaluate("__reorder(0,3)" if kind == "order" else "__toggle(1);__save()")
                    page.clock.run_for(450)
                    page.evaluate(f"__net.calls[0].res({{ok:true,status:200,json:()=>{outcome}}})")
                    page.clock.run_for(15050 if outcome.startswith("new") else 20)
                    pending = "pendingOrder" if kind == "order" else "pendingSel"
                    self.assertIsNotNone(page.evaluate(pending))
                    self.assertTrue(page.evaluate("__unload()"))
                    self.assertEqual(page.evaluate("__rejections"), [])

    def test_malformed_error_body_does_not_wedge_a_flight_or_loop_permanently(self):
        for kind in ("order", "save"):
            page = self.open(items=fx.synth(6), clock=True)
            page.evaluate("__reorder(0,3)" if kind == "order" else "__toggle(1);__save()")
            page.clock.run_for(450)
            page.evaluate("__net.calls[0].res({ok:false,status:400,json:()=>Promise.resolve(null)})")
            page.clock.run_for(60000)
            self.assertEqual(len(self.sent(page, f"/api/{kind}")), 1)
            flight = "orderFlight" if kind == "order" else "selFlight"
            self.assertEqual(page.evaluate(flight), 0)
            self.assertTrue(page.evaluate("__unload()"))
            self.assertEqual(page.evaluate("__rejections"), [])
