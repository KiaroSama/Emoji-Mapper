"""Regression tests for the rebuild state machine in coins/rebuild_dedup.py.

Each test pins one way the rebuild used to keep mutating packs after it had
stopped knowing what was live:

* an ambiguous upload was followed by the next upload, whose in-flight marker
  overwrote the record of the unresolved one (C-05),
* a failed live read counted as "0 stickers", which rolls the cursor back onto
  an entry that already landed (C-06),
* an old pack that survived deletion still completed the delete phase (H-15),
* a truncated plan file was read as the whole (frozen) plan (M-13),
* two runs could mutate one pack family at the same time (H-05).

No network and no real sleeps: Telegram is a fake object.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import build_pack as bp  # noqa: E402
from coins import rebuild_dedup as rd  # noqa: E402


def _png(path: Path, color=(10, 20, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (100, 100), color).save(path, "PNG")


class FakeTelegram:
    """Records mutations; every live answer is scripted per test."""

    def __init__(self, live: dict[str, int] | None = None):
        self.live = dict(live or {})          # set name -> live sticker count
        self.unknown: set[str] = set()        # sets whose live state is unreadable
        self.missing: set[str] = set()        # sets that are definitively gone
        self.add_calls: list[tuple] = []
        self.create_calls: list[tuple] = []
        self.deleted: list[str] = []
        self.messages: list[str] = []
        self.add_error: BaseException | None = None
        self.create_error: BaseException | None = None
        self.delete_error: BaseException | None = None

    # --- live state ---------------------------------------------------- #
    def probe_set_state(self, name: str):
        if name in self.unknown:
            return bp.SetState.UNKNOWN, None
        if name in self.missing or name not in self.live:
            return bp.SetState.MISSING, None
        return bp.SetState.EXISTS, {
            "stickers": [{"custom_emoji_id": f"{name}-{i}"}
                         for i in range(self.live[name])]}

    def probe_sticker_set(self, name: str):
        state, sset = self.probe_set_state(name)
        return state is not bp.SetState.UNKNOWN, sset

    def live_count_strict(self, name: str) -> int:
        state, sset = self.probe_set_state(name)
        if state is bp.SetState.UNKNOWN:
            raise bp.LiveStateUnknown(f"live state of {name} is unknown")
        return len(sset.get("stickers", [])) if sset else 0

    # --- mutations ------------------------------------------------------ #
    def add_sticker(self, user_id, name, png, emoji, kw, *, expected_before=None):
        self.add_calls.append((name, png.stem))
        if self.add_error:
            raise self.add_error
        self.live[name] = self.live.get(name, 0) + 1

    def create_set(self, user_id, name, title, png, emoji, kw):
        self.create_calls.append((name, png.stem))
        if self.create_error:
            raise self.create_error
        self.live[name] = 1

    def _call(self, method, *, data=None, **kw):
        if method == "deleteStickerSet":
            self.deleted.append(data["name"])
            if self.delete_error:
                raise self.delete_error
            self.live.pop(data["name"], None)
            self.missing.add(data["name"])
            return {}
        if method == "sendMessage":
            self.messages.append(data["text"])
            return {}
        if method == "getStickerSet":
            # Same contract as the real client: a missing set is an error here,
            # not an empty set.
            _, sset = self.probe_set_state(data["name"])
            if sset is None:
                raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
            return sset
        raise AssertionError(f"unexpected API call {method}")

    @property
    def mutations(self) -> int:
        return len(self.add_calls) + len(self.create_calls)


class RebuildCase(unittest.TestCase):
    """Redirects every module path at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.emoji = self.dir / "emoji"
        self.state = self.dir / "state.json"
        self.plan = self.dir / "plan.json"
        self.old_state = self.dir / "old_state.json"
        self.groups = self.dir / "shared_logo_groups.json"
        self.inv = self.dir / "inventory.md"
        self.inv.write_text("ticker: aaa\n", encoding="utf-8")
        # Every path the module writes to must point INSIDE the temp directory:
        # build_plan() rewrites the plan AND the shared-logo report, and those
        # are tracked project files.
        patches = [
            mock.patch.object(rd, "EMOJI", self.emoji),
            mock.patch.object(rd, "STATE", self.state),
            mock.patch.object(rd, "PLAN", self.plan),
            mock.patch.object(rd, "OLD_STATE", self.old_state),
            mock.patch.object(rd, "GROUPS_REPORT", self.groups),
            mock.patch.object(rd, "INV", self.inv),
            mock.patch.object(rd, "KEYWORDS_CSV", self.dir / "keywords.csv"),
            mock.patch.object(rd, "LOCK", self.dir / "state.json.lock"),
            mock.patch.object(rd, "USER_ID", 42),
            mock.patch.object(rd.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp_cleanup = self.addCleanup(self.tmp.cleanup)

    def write_plan(self, reps: list[str]) -> None:
        bp.write_json_atomic(self.plan, [
            {"rep": r, "tickers": [r], "kw": r, "hash": r} for r in reps])
        for r in reps:
            _png(self.emoji / f"{r}.png", (10 + 7 * len(r), 40, 90, 255))

    def write_state(self, **kw) -> dict:
        state = {"sets": [], "sent": [], "deleted_old": True, "final_sent": False,
                 "order": [], "cursor": 0, "in_flight": None}
        state.update(kw)
        bp.write_json_atomic(self.state, state)
        return state

    def saved(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def run_build(self, tg) -> object:
        """Run build(); return its exit code, or None if it did not stop.

        Asserting on the code last keeps the FIRST failure about what was
        mutated, which is the defect -- not about the exit code.
        """
        try:
            rd.build(tg, "bot")
        except SystemExit as exc:
            return exc.code
        return None


class AmbiguousUploadStopsTheRun(RebuildCase):
    """C-05: an unresolved mutation must be the LAST mutation of the run.

    Without the stop the loop continued, and the next entry's in-flight marker
    overwrote the unresolved one -- so nothing recorded which upload might have
    landed, and the reconcile on the next run attributed it to the wrong image.
    """

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa", "bbb"])
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=["seed"], cursor=0)
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
        self.write_state(order=[], cursor=0)
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
        tg = FakeTelegram(live={created: 1})   # the create did land
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


class LiveStateUnknownStopsTheRun(RebuildCase):
    """C-06: a failed live read is not "the set is empty"."""

    def setUp(self):
        super().setUp()
        self.write_plan(["aaa", "bbb"])
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
        rd.build(FakeTelegram(), "bot")
        self.assertFalse(rd.LOCK.exists())

    def test_the_lock_is_released_after_a_stop(self):
        self.write_plan(["aaa"])
        self.write_state()
        tg = FakeTelegram()
        tg.create_error = bp.AmbiguousUploadError("createNewStickerSet: unknown")
        with self.assertRaises(SystemExit):
            rd.build(tg, "bot")
        self.assertFalse(rd.LOCK.exists(),
                         "a stopped run must not block the retry")


if __name__ == "__main__":
    unittest.main()
