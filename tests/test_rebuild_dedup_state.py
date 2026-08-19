"""Regression tests for the rebuild state machine in coins/rebuild_dedup.py.

Each test pins one way the rebuild used to keep mutating packs after it had
stopped knowing what was live:

* an ambiguous upload was followed by the next upload, whose in-flight marker
  overwrote the record of the unresolved one (C-05),
* a failed live read counted as "0 stickers", which rolls the cursor back onto
  an entry that already landed (C-06),
* an old pack that survived deletion still completed the delete phase (H-15),
* a truncated plan file was read as the whole (frozen) plan (M-13),
* two runs could mutate one pack family at the same time (H-05),
* a retryable upload failure consuming the plan position forever (12),
* an in-flight upload judged "landed" by a count any stranger's sticker
  satisfies (13),
* a legacy in-flight marker accepted at validation and then unresolvable
  forever at recovery (B),
* a create recovery adopting a set of the wrong SHAPE, after it had already
  written the adoption to disk (C).

This module covers the mutation walk: plan -> validate -> delete the old packs
-> upload. The canonical map that the walk hands off to is a separate phase of
the same module and lives in `test_rebuild_dedup_map`.

No network and no real sleeps: Telegram is a fake object.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_pack as bp  # noqa: E402
from coins import rebuild_dedup as rd  # noqa: E402
from tests._rebuild_fixtures import (  # noqa: E402
    FakeTelegram, RebuildCase, _png, _png_bytes)


class AmbiguousUploadStopsTheRun(RebuildCase):
    """C-05: an unresolved mutation must be the LAST mutation of the run.

    Without the stop the loop continued, and the next entry's in-flight marker
    overwrote the unresolved one -- so nothing recorded which upload might have
    landed, and the reconcile on the next run attributed it to the wrong image.
    """

    def setUp(self):
        super().setUp()
        # "seed" is plan[0], already uploaded: order and cursor have to describe
        # the same walk of the plan, so a live sticker needs a plan entry that
        # accounts for it.
        self.write_plan(["seed", "aaa", "bbb"])
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=["seed"], cursor=1)
        self.tg = FakeTelegram(live={"s1": 1})
        self.tg.add_error = bp.AmbiguousUploadError("addStickerToSet: unknown")

    def test_the_second_entry_is_never_uploaded(self):
        code = self.run_build(self.tg)
        self.assertEqual(self.tg.mutations, 1,
                         "no mutation may follow an unresolved one")
        self.assertEqual([c[1] for c in self.tg.add_calls], ["aaa"])
        self.assertEqual(code, bp.EXIT_PARTIAL,
                         "an unresolved upload must ask the loop to resume")

    def test_the_unresolved_mutation_keeps_its_identity(self):
        self.run_build(self.tg)
        marker = self.saved()["in_flight"]
        self.assertEqual(marker["key"], "aaa",
                         "a later mutation overwrote the unresolved one")
        self.assertEqual(marker["operation"], "add")
        self.assertEqual(marker["set_name"], "s1")
        self.assertEqual(marker["set_index"], 1)
        self.assertEqual(marker["expected_before"], 1)
        self.assertEqual(marker["phase"], "upload")

    def test_an_ambiguous_create_records_the_set_it_may_have_made(self):
        # Empty state: the first entry has to create a set.
        self.write_state(sets=[], order=[], cursor=0)
        tg = FakeTelegram()
        tg.create_error = bp.AmbiguousUploadError("createNewStickerSet: unknown")
        code = self.run_build(tg)
        self.assertEqual(tg.mutations, 1)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        marker = self.saved()["in_flight"]
        self.assertEqual(marker["operation"], "create")
        self.assertEqual(marker["set_name"], f"{rd.BASE}1_by_bot")
        self.assertEqual(marker["set_index"], 1)
        self.assertEqual(self.saved()["sets"], [],
                         "the set is unconfirmed; it must not be recorded as ours")


class ResumeReconcilesTheMarker(RebuildCase):
    """The structured marker is what makes an ambiguous create recoverable."""

    def test_a_create_that_landed_is_adopted_from_the_marker(self):
        self.write_plan(["aaa"])
        created = f"{rd.BASE}1_by_bot"
        self.write_state(sets=[], order=[], cursor=1, in_flight={
            "key": "aaa", "operation": "create", "set_name": created,
            "set_index": 1, "expected_before": 0, "phase": "upload"})
        tg = FakeTelegram()                    # the create did land...
        tg.append(created, (self.emoji / "aaa.png").read_bytes())  # ...with OUR image
        rd.build(tg, "bot")
        saved = self.saved()
        self.assertEqual(saved["order"], ["aaa"], "the upload must be recorded")
        self.assertEqual([s["name"] for s in saved["sets"]], [created])
        self.assertIsNone(saved["in_flight"])
        self.assertEqual(tg.mutations, 0, "it already landed; do not re-send it")

    def test_a_create_that_did_not_land_is_retried_once(self):
        self.write_plan(["aaa"])
        created = f"{rd.BASE}1_by_bot"
        self.write_state(sets=[], order=[], cursor=1, in_flight={
            "key": "aaa", "operation": "create", "set_name": created,
            "set_index": 1, "expected_before": 0, "phase": "upload"})
        tg = FakeTelegram()                    # nothing exists live
        rd.build(tg, "bot")
        self.assertEqual([c[1] for c in tg.create_calls], ["aaa"])
        self.assertEqual(self.saved()["order"], ["aaa"])

    def test_an_unreadable_set_stops_instead_of_deciding(self):
        self.write_plan(["aaa"])
        created = f"{rd.BASE}1_by_bot"
        self.write_state(sets=[], order=[], cursor=1, in_flight={
            "key": "aaa", "operation": "create", "set_name": created,
            "set_index": 1, "expected_before": 0, "phase": "upload"})
        tg = FakeTelegram()
        tg.unknown.add(created)
        before = self.saved()
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(caught.exception.code, bp.EXIT_PARTIAL)
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(self.saved(), before, "state must be untouched")


class InFlightIsResolvedByIdentity(RebuildCase):
    """13: "the set grew by one" is not proof that OUR upload grew it.

    A sticker added by hand, or by a concurrent tool, satisfies the count just
    as well -- and the resume then records the plan entry as uploaded, so the
    image is never sent and its ticker is mapped onto a stranger's sticker.
    """

    MARKER = {"key": "aaa", "operation": "add", "set_name": "s1",
              "set_index": 1, "expected_before": 1, "phase": "upload"}

    def setUp(self):
        super().setUp()
        # plan[0]="seed" is already live; the marker is plan[1], the entry the
        # cursor was moved past to run it.
        self.write_plan(["seed", "aaa"])
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=["seed"], cursor=2, in_flight=dict(self.MARKER))

    def test_a_manual_sticker_cannot_satisfy_the_marker(self):
        tg = FakeTelegram(live={"s1": 1})
        tg.append("s1", _png_bytes("added-by-hand"))   # not our image
        self.run_build(tg)
        saved = self.saved()
        self.assertNotIn("aaa", saved["order"],
                         "a stranger's sticker was accepted as our upload")
        self.assertEqual(saved["cursor"], 1, "the entry must be retried")
        self.assertIsNone(saved["in_flight"])
        self.assertEqual(tg.mutations, 0,
                         "an unexplained sticker must stop the run, not be "
                         "built upon")

    def test_our_own_image_is_recognised(self):
        tg = FakeTelegram(live={"s1": 1})
        tg.append("s1", (self.emoji / "aaa.png").read_bytes())  # it did land
        rd.build(tg, "bot")
        saved = self.saved()
        self.assertEqual(saved["order"], ["seed", "aaa"])
        self.assertEqual(tg.mutations, 0, "it landed; do not send it again")
        self.assertIsNone(saved["in_flight"])

    def test_an_upload_that_never_landed_is_retried(self):
        tg = FakeTelegram(live={"s1": 1})            # nothing was added
        rd.build(tg, "bot")
        self.assertEqual([c[1] for c in tg.add_calls], ["aaa"])
        self.assertEqual(self.saved()["order"], ["seed", "aaa"])

    def test_an_unreadable_set_stops_instead_of_deciding(self):
        tg = FakeTelegram(live={"s1": 1})
        tg.unknown.add("s1")
        before = self.saved()
        code = self.run_build(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(self.saved(), before, "state must be untouched")


class CreateIsAdoptedOnlyOnImageIdentity(RebuildCase):
    """4: EXISTS is not proof that OUR create made the set.

    The reconcile treated "a set of that name is live" as "the in-flight create
    landed" and adopted it. A same-named set can pre-exist -- a leftover family,
    a hand-made pack, another bot's -- and adopting it attaches this rebuild's
    state, its cursor and every later add to a pack it never built. Only the
    first sticker's IMAGE can tell the two apart.
    """

    CREATED = f"{rd.BASE}1_by_bot"

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa", "bbb"])
        self.write_state(sets=[], order=[], cursor=1, in_flight={
            "key": "aaa", "operation": "create", "set_name": self.CREATED,
            "set_index": 1, "expected_before": 0, "phase": "upload"})

    def test_a_foreign_first_image_is_never_adopted(self):
        tg = FakeTelegram()
        tg.append(self.CREATED, _png_bytes("someone-elses-pack"))
        before = self.saved()
        code = self.run_build(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL,
                         "an unidentifiable set must stop the run")
        self.assertEqual(tg.mutations, 0, "nothing may be built on it")
        self.assertEqual(self.saved(), before,
                         "the set is not ours: it must not enter the state")

    def test_a_first_sticker_that_cannot_be_read_stops_the_run(self):
        tg = FakeTelegram()
        tg.append(self.CREATED, b"not an image at all")
        before = self.saved()
        code = self.run_build(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL,
                         "unverifiable is UNKNOWN, never 'close enough'")
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(self.saved(), before)

    def test_an_empty_set_of_that_name_is_not_treated_as_ours(self):
        """A create leaves exactly one sticker; nothing else is our create."""
        tg = FakeTelegram()
        tg.images[self.CREATED] = []
        tg.live[self.CREATED] = 0
        before = self.saved()
        self.assertEqual(self.run_build(tg), bp.EXIT_PARTIAL)
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(self.saved(), before)

    def test_our_image_beside_a_foreign_one_is_not_our_create(self):
        """C: the SHAPE has to be checked before anything is written.

        A create leaves EXACTLY one sticker, so [OURS, FOREIGN] is somebody
        else's set -- and it satisfies the first-sticker check. The count
        disagreement only surfaced afterwards, in the cum/order comparison,
        with the adoption, the cursor and the order record already saved.
        """
        tg = FakeTelegram()
        tg.append(self.CREATED, (self.emoji / "aaa.png").read_bytes())   # ours
        tg.append(self.CREATED, _png_bytes("someone-elses-second"))      # not
        before = self.saved()
        code = self.run_build(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL,
                         "a two-sticker set is not the one our create made")
        self.assertEqual(tg.mutations, 0)
        saved = self.saved()
        self.assertEqual(saved["sets"], [],
                         "a set of the wrong shape must not be adopted")
        self.assertEqual(saved["cursor"], before["cursor"])
        self.assertEqual(saved["order"], [])
        self.assertEqual(saved["in_flight"], before["in_flight"],
                         "the intent must survive to be resolved next run")
        self.assertEqual(saved, before, "the refusal half-applied itself")

    def test_our_own_image_is_still_adopted(self):
        tg = FakeTelegram()
        tg.append(self.CREATED, (self.emoji / "aaa.png").read_bytes())
        rd.build(tg, "bot")
        saved = self.saved()
        self.assertEqual([s["name"] for s in saved["sets"]], [self.CREATED])
        self.assertEqual(saved["order"], ["aaa", "bbb"],
                         "the adopted upload counts; the run continues from it")
        self.assertEqual([c[1] for c in tg.add_calls], ["bbb"],
                         "aaa is already live and must not be sent again")


class CursorOrderAndPlanMustDescribeOneWalk(RebuildCase):
    """5: cursor, order and the plan were only ever checked for shape.

    cursor=0 with order=["aaa"] and one live sticker satisfied every existing
    invariant, and the build then started at plan[0] and uploaded "aaa" a
    SECOND time. The mirror case is a cursor at the end with an order that
    cannot have come from that prefix: plan entries are skipped and the run
    reports a completed rebuild.
    """

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa", "bbb", "ccc"])
        bp.write_json_atomic(self.old_state, {"sets": [{"name": "old1"}]})

    def _rejects(self, **state) -> str:
        self.write_state(**{"deleted_old": False, **state})
        tg = FakeTelegram(live={"old1": 3, "s1": 1})
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(tg.deleted, [],
                         "an inconsistent state must be caught BEFORE the "
                         "destructive delete phase")
        self.assertEqual(tg.mutations, 0)
        return str(caught.exception.code)

    def test_an_upload_the_cursor_never_reached_is_rejected(self):
        # THE case: one live sticker, one recorded upload, cursor still at 0.
        # Resuming here re-uploads plan[0] into a set that already holds it.
        problem = self._rejects(
            cursor=0, order=["aaa"],
            sets=[{"index": 1, "name": "s1", "title": "T 1", "live": 1}])
        self.assertIn("'order' records 'aaa'", problem)

    def test_an_order_that_is_not_in_plan_order_is_rejected(self):
        self.assertIn("'order' records 'aaa'", self._rejects(
            cursor=3, order=["ccc", "aaa"],
            sets=[{"index": 1, "name": "s1", "title": "T 1", "live": 2}]))

    def test_an_upload_that_is_not_a_plan_entry_at_all_is_rejected(self):
        self.assertIn("'order' records 'zzz'", self._rejects(
            cursor=3, order=["zzz"],
            sets=[{"index": 1, "name": "s1", "title": "T 1", "live": 1}]))

    def test_uploads_with_no_set_to_live_in_are_rejected(self):
        self.assertIn("no set holds them",
                      self._rejects(cursor=1, order=["aaa"], sets=[]))

    def test_a_marker_for_a_different_plan_entry_is_rejected(self):
        # The cursor is moved past an entry before its mutation is recorded, so
        # a marker for anything else reconciles one entry's outcome onto another.
        self.assertIn("is not plan entry", self._rejects(
            cursor=1, order=[], sets=[{"index": 1, "name": "s1", "live": 0}],
            in_flight={"key": "ccc", "operation": "add", "set_name": "s1",
                       "set_index": 1, "expected_before": 0}))

    def test_a_marker_with_no_cursor_behind_it_is_rejected(self):
        self.assertIn("is not plan entry", self._rejects(
            cursor=0, order=[], sets=[{"index": 1, "name": "s1", "live": 0}],
            in_flight={"key": "aaa", "operation": "add", "set_name": "s1",
                       "set_index": 1, "expected_before": 0}))

    def test_live_drift_stops_before_the_old_packs_are_deleted(self):
        """The one inconsistency only LIVE state can see, checked in time.

        Nothing on disk is wrong here, so the schema cannot catch it: the set
        simply holds a sticker no upload accounts for. The reconciliation that
        does catch it used to run after the delete phase, so the old packs were
        already destroyed by the time the run refused to continue.
        """
        self.write_state(deleted_old=False, cursor=1, order=["aaa"],
                         sets=[{"index": 1, "name": "s1", "title": "T 1",
                                "live": 1}])
        tg = FakeTelegram(live={"old1": 3, "s1": 2})   # one sticker too many
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertIn("uploads are recorded", str(caught.exception.code))
        self.assertEqual(tg.deleted, [],
                         "the old packs were destroyed on a state that could "
                         "not be reconciled")
        self.assertEqual(tg.mutations, 0)

    def test_a_walk_with_skipped_entries_is_still_accepted(self):
        """Skips are legitimate: order is a SUBSEQUENCE, not a copy."""
        self.write_state(order=["aaa", "ccc"], cursor=3,
                         sets=[{"index": 1, "name": "s1", "title": "T 1",
                                "live": 2}])
        tg = FakeTelegram(live={"s1": 2})
        rd.build(tg, "bot")                       # bbb was skipped; nothing left
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(self.saved()["cursor"], 3)


class RetryableFailureKeepsThePlanPosition(RebuildCase):
    """12: the cursor moves past an entry BEFORE the upload is attempted.

    A failure that definitely did not apply must put it back, or that image is
    skipped forever -- silently, because the run still ends "successfully".
    """

    def setUp(self):
        super().setUp()
        self.write_plan(["seed", "aaa", "bbb"])
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=["seed"], cursor=1)
        self.tg = FakeTelegram(live={"s1": 1})

    def test_a_transport_failure_leaves_the_cursor_on_the_item(self):
        self.tg.add_error = RuntimeError(
            "addStickerToSet failed after 5 attempts")
        code = self.run_build(self.tg)
        saved = self.saved()
        self.assertEqual(saved["cursor"], 1,
                         "a not-applied failure must not consume the position")
        self.assertEqual(saved["order"], ["seed"])
        self.assertEqual(self.tg.mutations, 1,
                         "the run must stop; walking on with a rolled-back "
                         "cursor re-uploads everything after it")
        self.assertEqual(code, bp.EXIT_PARTIAL)

    def test_a_media_rejection_is_still_skipped_for_good(self):
        self.tg.add_error = RuntimeError(
            "addStickerToSet failed: Bad Request: STICKER_PNG_NOPNG")
        code = self.run_build(self.tg)
        saved = self.saved()
        self.assertEqual(saved["cursor"], 3, "both images are permanently bad")
        self.assertEqual(saved["order"], ["seed"])
        self.assertEqual(self.tg.mutations, 2, "each entry is tried once")
        self.assertIsNone(code, "a permanent skip is not a retryable stop")


class LinkMessagesAreBounded(RebuildCase):
    """19: sendMessage has no dedup key, so five retries can post five links."""

    def test_the_link_message_retries_at_most_twice(self):
        self.write_plan(["aaa"])
        self.write_state()
        tg = FakeTelegram()
        rd.build(tg, "bot")
        self.assertEqual(len(tg.messages), 1)
        self.assertEqual(tg.message_retries, [2],
                         "the default retry count multiplies accepted posts")
        self.assertEqual(tg.message_previews, [True],
                         "a preview card per addemoji link buries the list; "
                         "the coin path had this before the shared announcer "
                         "and must not have lost it")


class OwnerIdIsParsedSafely(unittest.TestCase):
    """18: int() on a .env typo raised before argparse could explain anything."""

    def test_a_typo_falls_back_instead_of_killing_the_import(self):
        self.addCleanup(importlib.reload, rd)
        with mock.patch.dict(os.environ, {"PACK_OWNER_USER_ID": "42abc"},
                             clear=False):
            self.assertEqual(importlib.reload(rd).USER_ID, 0)

    def test_a_valid_value_is_still_used(self):
        self.addCleanup(importlib.reload, rd)
        with mock.patch.dict(os.environ, {"PACK_OWNER_USER_ID": "12345"},
                             clear=False):
            self.assertEqual(importlib.reload(rd).USER_ID, 12345)


class RebuildTakesThePackFamilyLock(unittest.TestCase):
    """6: a lock named after this tool's state file excludes nobody else."""

    def test_the_lock_is_keyed_on_the_pack_base(self):
        self.assertEqual(rd.LOCK, bp.pack_family_lock_path(rd.BASE))


class LiveStateUnknownStopsTheRun(RebuildCase):
    """C-06: a failed live read is not "the set is empty"."""

    def setUp(self):
        super().setUp()
        self.write_plan(["seed", "aaa", "bbb"])
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=["seed"], cursor=1)
        self.tg = FakeTelegram(live={"s1": 1})
        self.tg.unknown.add("s1")

    def test_cursor_order_and_marker_are_left_untouched(self):
        before = self.saved()
        with self.assertRaises(SystemExit) as caught:
            rd.build(self.tg, "bot")
        self.assertEqual(caught.exception.code, bp.EXIT_PARTIAL)
        self.assertEqual(self.tg.mutations, 0,
                         "an unknown live count must not drive an upload")
        after = self.saved()
        self.assertEqual(after["cursor"], before["cursor"])
        self.assertEqual(after["order"], before["order"])
        self.assertIsNone(after["in_flight"])
        self.assertEqual(after, before)


class OldPackDeletionMustBeConfirmed(RebuildCase):
    """H-15: a pack that survived deletion used to complete the phase."""

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa"])
        self.write_state(deleted_old=False)
        bp.write_json_atomic(self.old_state,
                             {"sets": [{"name": "old1"}, {"name": "old2"}]})

    def test_a_surviving_pack_leaves_the_phase_open(self):
        tg = FakeTelegram(live={"old1": 5, "old2": 5})
        tg.delete_error = RuntimeError("deleteStickerSet failed: BOT_ACCESS_DENIED")
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(caught.exception.code, bp.EXIT_PARTIAL)
        saved = self.saved()
        self.assertFalse(saved["deleted_old"],
                         "the phase must stay open while a pack is still live")
        self.assertEqual(saved.get("deleted_old_packs", []), [])
        self.assertEqual(tg.mutations, 0, "never build beside surviving packs")

    def test_partial_deletion_records_only_what_is_gone(self):
        tg = FakeTelegram(live={"old1": 5, "old2": 5})
        real_call = tg._call

        def only_first(method, *, data=None, **kw):
            if method == "deleteStickerSet" and data["name"] == "old2":
                raise RuntimeError("deleteStickerSet failed: BOT_ACCESS_DENIED")
            return real_call(method, data=data, **kw)

        tg._call = only_first
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(caught.exception.code, bp.EXIT_PARTIAL)
        saved = self.saved()
        self.assertEqual(saved.get("deleted_old_packs"), ["old1"],
                         "only the confirmed-gone pack may be checked off")
        self.assertFalse(saved["deleted_old"])
        self.assertEqual(tg.mutations, 0)

    def test_an_unreadable_pack_stops_the_run(self):
        tg = FakeTelegram(live={"old1": 5, "old2": 5})
        tg.unknown.add("old1")
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(caught.exception.code, bp.EXIT_PARTIAL)
        self.assertEqual(tg.deleted, ["old1"], "stop at the first unknown answer")
        self.assertFalse(self.saved()["deleted_old"])

    def test_a_confirmed_deletion_completes_the_phase(self):
        tg = FakeTelegram(live={"old1": 5, "old2": 5})
        rd.build(tg, "bot")
        saved = self.saved()
        self.assertTrue(saved["deleted_old"])
        self.assertEqual(saved.get("deleted_old_packs"), ["old1", "old2"])
        self.assertEqual([c[1] for c in tg.create_calls], ["aaa"])

    def test_deletion_is_not_repeated_after_a_resume(self):
        tg = FakeTelegram(live={"old1": 5, "old2": 5})
        tg.unknown.add("old2")
        with self.assertRaises(SystemExit):
            rd.build(tg, "bot")
        self.assertEqual(tg.deleted, ["old1", "old2"])
        tg.unknown.clear()
        rd.build(tg, "bot")
        self.assertEqual(tg.deleted, ["old1", "old2", "old2"],
                         "old1 was confirmed gone and must not be re-deleted")


class TruncatedPlanFailsClosed(RebuildCase):
    """M-13: the frozen plan must never be half-read or silently regenerated."""

    def test_a_truncated_plan_refuses_to_build(self):
        self.plan.write_text('[{"rep": "aaa", "tickers": ["aa',
                             encoding="utf-8")
        self.write_state()
        tg = FakeTelegram()
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertIn(self.plan.name, str(caught.exception.code))
        self.assertEqual(tg.mutations, 0)

    def test_a_plan_entry_missing_its_fields_is_rejected(self):
        bp.write_json_atomic(self.plan, [{"rep": "aaa", "tickers": ["aaa"],
                                          "kw": "aaa"},
                                         {"rep": "bbb"}])
        self.write_state()
        tg = FakeTelegram()
        with self.assertRaises(SystemExit):
            rd.build(tg, "bot")
        self.assertEqual(tg.mutations, 0)

    def test_a_sound_plan_still_loads(self):
        self.write_plan(["aaa", "bbb"])
        self.assertEqual([g["rep"] for g in rd.load_plan()], ["aaa", "bbb"])

    def test_the_plan_and_the_report_are_written_atomically(self):
        """A killed process cannot be staged in-process; the writer is the contract.

        write_text truncates the live file first, so an interrupted generation
        destroys the frozen plan. write_json_atomic renames a finished temp file
        over it, so the previous plan survives any crash before the rename.
        """
        _png(self.emoji / "aaa.png")
        with mock.patch.object(rd, "write_json_atomic",
                               side_effect=bp.write_json_atomic) as writer:
            rd.build_plan()
        self.assertEqual({c.args[0] for c in writer.call_args_list},
                         {self.plan, self.groups})
        self.assertEqual([g["rep"] for g in rd.load_plan()], ["aaa"])
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.tmp")), [])


class _LockWatchingTelegram(FakeTelegram):
    """Records whether the pack-family lock was held at the first mutation.

    Without this, both release tests below assert only that the lock file is
    ABSENT when the run ends -- which an implementation that never takes the
    lock at all satisfies perfectly. Sampling at the mutation is what makes them
    prove acquire-and-release rather than merely "no leak".
    """

    held_at_mutation: bool | None = None

    def _sample(self) -> None:
        if self.held_at_mutation is None:
            self.held_at_mutation = rd.LOCK.exists()

    def create_set(self, *a, **kw):
        self._sample()
        return super().create_set(*a, **kw)

    def add_sticker(self, *a, **kw):
        self._sample()
        return super().add_sticker(*a, **kw)


class ConcurrentRunsAreLockedOut(RebuildCase):
    """H-05: two publishers on one state file upload the same entries twice."""

    def test_a_second_run_refuses_to_start(self):
        self.write_plan(["aaa"])
        self.write_state()
        tg = FakeTelegram()
        with bp.exclusive_lock(rd.LOCK):
            with self.assertRaises(bp.LockBusy):
                rd.build(tg, "bot")
        self.assertEqual(tg.mutations, 0)

    def test_the_lock_is_released_after_a_run(self):
        self.write_plan(["aaa"])
        self.write_state()
        tg = _LockWatchingTelegram()
        rd.build(tg, "bot")
        self.assertTrue(tg.held_at_mutation,
                        "the lock must be HELD while the packs are mutated")
        self.assertFalse(rd.LOCK.exists())

    def test_the_lock_is_released_after_a_stop(self):
        self.write_plan(["aaa"])
        self.write_state()
        tg = _LockWatchingTelegram()
        tg.create_error = bp.AmbiguousUploadError("createNewStickerSet: unknown")
        with self.assertRaises(SystemExit):
            rd.build(tg, "bot")
        self.assertTrue(tg.held_at_mutation,
                        "the lock must be HELD while the packs are mutated")
        self.assertFalse(rd.LOCK.exists(),
                         "a stopped run must not block the retry")


class StateSchemaIsValidatedBeforeAnyMutation(RebuildCase):
    """7: load_state() parsed JSON and checked no invariant at all.

    cursor=-1 makes plan[-1] the first upload AND leaves it to be uploaded
    again at the end; a cursor past the plan reports the rebuild finished
    without ever walking it. Both had to be caught before the delete phase.
    """

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa", "bbb"])
        bp.write_json_atomic(self.old_state, {"sets": [{"name": "old1"}]})

    def _rejects(self, **state) -> str:
        self.write_state(**{"deleted_old": False, **state})
        tg = FakeTelegram(live={"old1": 3})
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")
        self.assertEqual(tg.deleted, [],
                         "an untrusted state must not destroy the old packs")
        self.assertEqual(tg.mutations, 0)
        self.assertEqual(tg.messages, [], "and must not announce anything")
        return str(caught.exception.code)

    def test_a_negative_cursor_is_rejected_before_any_deletion(self):
        self.assertIn("cursor -1", self._rejects(cursor=-1))

    def test_an_oversized_cursor_is_rejected_instead_of_reporting_done(self):
        self.assertIn("past the end", self._rejects(cursor=3))

    def test_a_non_integer_cursor_is_rejected(self):
        self.assertIn("cursor", self._rejects(cursor="1"))

    def test_a_boolean_cursor_is_not_read_as_a_number(self):
        self.assertIn("cursor", self._rejects(cursor=True))

    def test_an_order_that_repeats_an_entry_is_rejected(self):
        self.assertIn("more than once", self._rejects(order=["aaa", "aaa"]))

    def test_a_repeated_set_name_is_rejected(self):
        self.assertIn("repeats the set name", self._rejects(sets=[
            {"index": 1, "name": "s1"}, {"index": 2, "name": "s1"}]))

    def test_set_indexes_must_ascend(self):
        self.assertIn("does not ascend", self._rejects(sets=[
            {"index": 2, "name": "s2"}, {"index": 1, "name": "s1"}]))

    def test_a_live_count_over_the_pack_limit_is_rejected(self):
        self.assertIn("outside 0..", self._rejects(sets=[
            {"index": 1, "name": "s1", "live": rd.PER_SET + 1}]))

    def test_an_in_flight_marker_missing_its_target_is_rejected(self):
        self.assertIn("set_name", self._rejects(in_flight={
            "key": "aaa", "operation": "add"}))

    def test_an_unknown_in_flight_operation_is_rejected(self):
        self.assertIn("unknown", self._rejects(in_flight={
            "key": "aaa", "operation": "delete", "set_name": "s1",
            "set_index": 1}))

    def test_wrong_types_are_rejected(self):
        self.assertIn("'order'", self._rejects(order="aaa"))
        self.assertIn("'deleted_old'", self._rejects(deleted_old="no"))

    def test_the_legacy_bare_key_marker_is_rejected_here_not_at_recovery(self):
        """B: it used to pass validation and then never be resolvable.

        A bare key names no set, so _reconcile_in_flight had nothing to probe
        and stopped with EXIT_PARTIAL -- every run, at the same point, with no
        way to clear it. It cannot be resolved by guessing either: the version
        that wrote it (5d4669b) recorded the key before choosing between add
        and create, so the set may never have reached state["sets"].
        """
        problem = self._rejects(in_flight="aaa")
        self.assertIn("legacy bare key", problem)
        self.assertIn("cursor", problem, "the operator needs the exact repair")
        self.assertIn("'order'", problem,
                      "the IS-live branch also has to name 'order', or the "
                      "repair walks into the cum/order gate and stops again")

    def test_following_the_legacy_repair_actually_clears_the_stop(self):
        """A prescribed repair that does not work is a second permanent stop.

        The message is only worth what executing it achieves, so execute it. The
        cursor is advanced past an entry BEFORE its mutation is recorded and
        'order' gains the key only on success -- so in the IS-live branch,
        clearing the marker alone leaves one more live sticker than recorded
        uploads, and build()'s own consistency gate refuses. Asserting on the
        wording of the message could never have caught that.
        """
        self.write_plan(["seed", "aaa", "bbb"])
        self.write_state(deleted_old=True, cursor=2, order=["seed"],
                         in_flight="aaa",
                         sets=[{"index": 1, "name": "s1", "title": "T 1"}])
        tg = FakeTelegram(live={"s1": 2})       # seed AND aaa are both live
        with self.assertRaises(SystemExit) as caught:
            rd.build(tg, "bot")                 # refused, as it must be

        # Read the repair OUT of the message before performing it. Hard-coding
        # the two edits proved that THIS repair works, not that the prescribed
        # one does -- so the message could drift into prescribing something else
        # and the test would still pass while the instructions went wrong.
        prescribed = str(caught.exception)
        self.assertIn("'in_flight' to null", prescribed)
        self.assertIn("append 'aaa' to 'order'", prescribed)

        state = json.loads(self.state.read_text("utf-8"))
        state["in_flight"] = None
        state["order"].append("aaa")
        bp.write_json_atomic(self.state, state)

        rd.build(tg, "bot")                     # must now run to completion
        after = json.loads(self.state.read_text("utf-8"))
        self.assertIsNone(after["in_flight"])
        self.assertIn("aaa", after["order"])
        sent = [str(c) for c in tg.add_calls + tg.create_calls]
        self.assertFalse([c for c in sent if "aaa" in c],
                         "the repair must not re-upload an image already live")

    def test_a_marker_that_is_neither_object_nor_null_is_rejected(self):
        self.assertIn("'in_flight'", self._rejects(in_flight=["aaa"]))

    def test_a_sound_state_still_builds(self):
        self.write_state(deleted_old=False, cursor=0)
        tg = FakeTelegram(live={"old1": 3})
        rd.build(tg, "bot")
        self.assertEqual(tg.deleted, ["old1"])
        self.assertEqual([c[1] for c in tg.create_calls], ["aaa"])
        self.assertEqual(self.saved()["cursor"], 2)


if __name__ == "__main__":
    unittest.main()
