"""Regression tests for the resume/duplicate-upload defects.

These lock down the paths that actually put the same emoji in a pack twice:

* a skipped source image shifting the resume cursor (build_pack + rebuild_dedup),
* a truncated JSON state file being read back as "nothing uploaded yet",
* a positional ticker->emoji-id map being written from drifted positions,
* the bot token reaching stdout through an exception message,
* STICKERSET_INVALID being retried for six minutes on a plain lookup.

No network and no real sleeps: every Telegram call is faked.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

import build_pack as bp  # noqa: E402


def _png(path: Path, color=(10, 20, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (100, 100), color).save(path, "PNG")


class AtomicJsonWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_and_leaves_no_temp_file(self):
        target = self.dir / "state.json"
        bp.write_json_atomic(target, {"done": ["a", "b"]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"done": ["a", "b"]})
        self.assertEqual(list(self.dir.iterdir()), [target])

    def test_previous_content_survives_a_failed_write(self):
        target = self.dir / "state.json"
        bp.write_json_atomic(target, {"done": ["a"]})
        # A crash mid-serialisation must not truncate the existing file.
        unserialisable = {"done": {1, 2}}
        with self.assertRaises(TypeError):
            bp.write_json_atomic(target, unserialisable)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"done": ["a"]})

    def test_overwrites_in_place(self):
        target = self.dir / "state.json"
        bp.write_json_atomic(target, {"n": 1})
        bp.write_json_atomic(target, {"n": 2})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"n": 2})


class TokenRedaction(unittest.TestCase):
    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_exception_text_never_carries_the_token(self):
        tg = bp.Telegram(self.TOKEN)
        exc = requests.ConnectionError(
            f"HTTPSConnectionPool: /bot{self.TOKEN}/addStickerToSet failed")
        out = tg._safe(exc)
        self.assertNotIn(self.TOKEN, out)
        self.assertIn("[REDACTED]", out)

    def test_network_retry_output_is_redacted(self):
        tg = bp.Telegram(self.TOKEN)
        boom = requests.ConnectionError(f"conn to /bot{self.TOKEN}/getMe reset")
        with mock.patch.object(tg.s, "post", side_effect=boom), \
             mock.patch.object(bp.time, "sleep", lambda s: None), \
             mock.patch("sys.stdout") as out:
            with self.assertRaises(RuntimeError):
                tg._call("getMe", retries=2)
        printed = "".join(c.args[0] for c in out.write.call_args_list if c.args)
        self.assertNotIn(self.TOKEN, printed)


class StickerSetInvalidScope(unittest.TestCase):
    """A missing set must answer at once; only a create waits for a name lock."""

    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def _session(self):
        resp = mock.Mock()
        resp.json.return_value = {"ok": False, "description": "STICKERSET_INVALID"}
        return resp

    def test_lookup_fails_fast_without_sleeping(self):
        tg = bp.Telegram(self.TOKEN)
        slept = []
        with mock.patch.object(tg.s, "post", return_value=self._session()), \
             mock.patch.object(bp.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                tg._call("getStickerSet", data={"name": "nope"})
        self.assertEqual(slept, [], "a plain lookup must not sleep on a missing set")

    def test_create_still_waits_for_the_name_lock(self):
        tg = bp.Telegram(self.TOKEN)
        slept = []
        with mock.patch.object(tg.s, "post", return_value=self._session()), \
             mock.patch.object(bp.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                tg._call("createNewStickerSet", data={"name": "x"}, retries=3)
        self.assertTrue(slept, "a create must still wait for a released name")
        self.assertLessEqual(sum(slept), bp.NAME_LOCK_TIMEOUT,
                             "waiting must be bounded by the deadline")
        self.assertEqual(len(slept), 2, "no sleep after the final attempt")


class PerSetLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _png(self.dir / "src" / "a.png")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.dir / "src"),
                "--token-env", "FAKE_TOKEN", "--dry-run", *extra]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False):
            return bp.main()

    def test_default_matches_telegram_cap(self):
        self.assertEqual(bp.PER_SET, 200)
        self.assertEqual(bp.MAX_PER_SET, 200)

    def test_zero_is_rejected_instead_of_dividing_by_zero(self):
        self.assertEqual(self._run("--per-set", "0"), 2)

    def test_above_the_cap_is_rejected(self):
        self.assertEqual(self._run("--per-set", "400"), 2)

    def test_negative_start_is_rejected(self):
        self.assertEqual(self._run("--start", "-5"), 2)

    def test_valid_run_succeeds(self):
        self.assertEqual(self._run("--per-set", "200"), 0)


class ResumeAfterSkippedImage(unittest.TestCase):
    """The defect: a skipped image shifts positional attribution by one.

    Sources are a.png (unusable), b.png, c.png. A previous run skipped a.png and
    uploaded b.png but crashed before recording it. Positional recovery blamed
    the drift on the first pending entry -- a.png -- marking the wrong image
    done and re-uploading b.png as a duplicate.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "a.png")
        (self.src / "a.png").write_bytes(b"")          # unusable -> skipped
        _png(self.src / "b.png", (200, 0, 0, 255))
        _png(self.src / "c.png", (0, 200, 0, 255))
        self.state = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_tg(self, live_count: int, *, matches=True):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        sset = {"stickers": [{"file_unique_id": f"f{i}"} for i in range(live_count)]}
        tg.probe_sticker_set.return_value = (True, sset)
        tg.probe_set_state.return_value = (bp.SetState.EXISTS, sset)
        tg.add_sticker.return_value = None
        tg.create_set.return_value = None
        tg.send_message.return_value = None
        if isinstance(matches, list):
            # Per-sticker verdicts, in the order the reconciler examines them:
            # "which of the arrivals is ours" is the question, so a fake that
            # can only answer one way for all of them cannot pose it.
            tg._sticker_matches.side_effect = list(matches)
        else:
            tg._sticker_matches.return_value = matches
        return tg

    def _run(self, tg):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def test_in_flight_record_attributes_the_upload_exactly(self):
        # b.png was applied but not recorded; the intent record names it.
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": "b",
        })
        tg = self._fake_tg(live_count=1)
        # a.png is an empty file, so the run is legitimately PARTIAL.
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)

        uploaded = [c.args[2].stem for c in tg.add_sticker.call_args_list]
        uploaded += [c.args[3].stem for c in tg.create_set.call_args_list]
        self.assertNotIn("b", uploaded, "b.png was already live and must not re-upload")
        self.assertIn("c", uploaded, "c.png is genuinely pending")

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"])
        self.assertIsNone(saved["in_flight"])

    def test_a_foreign_tail_sticker_does_not_resolve_our_in_flight_add(self):
        """+1 on restart is not proof either.

        The set grew by one while our add was unresolved -- but by someone
        else's sticker. Marking our item done here binds our source to theirs.
        """
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": {"key": "b", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 0},
        })
        tg = self._fake_tg(live_count=1, matches=False)
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("b", saved["done"],
                         "a stranger's sticker must not mark our item done")

    def test_ours_behind_a_foreign_sticker_is_reconciled_not_stranded(self):
        """expected + 2 is the NORMAL shape of the sequence this exists for.

        Our attempt fails, a foreign sticker lands, our retry succeeds: the set
        is two bigger than the snapshot. Demanding expected or expected+1 made
        that a permanent EXIT_FAILED -- our sticker live, unrecorded, on every
        subsequent run, one transient blip turned terminal. What decides it is
        whether OUR image is among the arrivals, not how many arrived.
        """
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": {"key": "b", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 0},
        })
        # Two stickers arrived; only the SECOND is ours.
        tg = self._fake_tg(live_count=2, matches=[False, True])
        self.assertNotEqual(self._run(tg), bp.EXIT_FAILED)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "our live sticker was left off the books")
        # The reconcile booked the size it actually SAW (2), not expected+1 (1);
        # the run then continued and published the remaining item on top of it.
        self.assertGreaterEqual(saved["sets"][0]["count"], 2,
                                "the recorded size came from our own arithmetic, "
                                "not from the set")

    def test_a_removal_masking_our_add_does_not_re_upload_a_live_image(self):
        """live_n == expected is NOT proof that nothing landed.

        A sticker was removed from the set and ours was added, so the size did
        not move. The reconciler used to read arrivals by POSITION -- the slice
        past `expected` -- which is empty here, so the content oracle was never
        consulted at all: the run announced "did not land", left the item
        pending and uploaded a SECOND copy of an image that was already live,
        then exited 0.
        """
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 2, "index": 1}],
            "in_flight": {"key": "b", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 2},
        })
        # Still two stickers: one of the originals is gone and OURS took its
        # place, so the only sticker that answers True sits INSIDE `expected`.
        tg = self._fake_tg(live_count=2, matches=[False, True])
        self.assertNotEqual(self._run(tg), bp.EXIT_FAILED)

        uploaded = [c.args[2].stem for c in tg.add_sticker.call_args_list]
        uploaded += [c.args[3].stem for c in tg.create_set.call_args_list]
        self.assertNotIn("b", uploaded,
                         "b.png is live in the set; uploading it again is the "
                         "duplicate this reconciler exists to prevent")

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "our live sticker was left off the books")
        self.assertIsNone(saved["in_flight"])

    def test_unexplained_drift_refuses_to_guess(self):
        # Two extra live stickers and no in-flight record: the old code silently
        # marked the first two pending images done, which is a coin-flip.
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": None,
        })
        tg = self._fake_tg(live_count=2)
        self.assertEqual(self._run(tg), bp.EXIT_FAILED,
                         "must refuse rather than mis-attribute")
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_corrupt_state_does_not_restart_from_zero(self):
        self.state.write_text('{"base": "t", "done": [trunca', encoding="utf-8")
        tg = self._fake_tg(live_count=0)
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_intent_is_recorded_before_each_upload(self):
        tg = self._fake_tg(live_count=0)
        seen = []

        def record(*a, **kw):
            seen.append(json.loads(self.state.read_text(encoding="utf-8"))["in_flight"])

        tg.create_set.side_effect = record
        tg.add_sticker.side_effect = record
        # a.png is unusable and skipped, so the run is PARTIAL, not clean.
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)
        # b and c upload, each announcing a STRUCTURED intent before the call.
        self.assertEqual([i["key"] for i in seen], ["b", "c"])
        self.assertEqual([i["operation"] for i in seen], ["create", "add"])
        self.assertTrue(all(i["set_name"] for i in seen),
                        "every intent must name the set it targets")


class ExitCodes(unittest.TestCase):
    """A command that achieved nothing must not report success.

    add_media/fetch_pack/fetch_emoji_ids all returned 0 unconditionally, so a
    run where every input failed looked identical to a clean one -- launchers
    and retry logic could not tell them apart.
    """

    def test_clean_run_is_zero(self):
        self.assertEqual(bp.ingest_exit_code(succeeded=5, failed=0), bp.EXIT_OK)

    def test_partial_run_is_retryable(self):
        self.assertEqual(bp.ingest_exit_code(succeeded=3, failed=2), bp.EXIT_PARTIAL)

    def test_total_failure_is_terminal(self):
        self.assertEqual(bp.ingest_exit_code(succeeded=0, failed=4), bp.EXIT_FAILED)

    def test_empty_run_is_not_a_failure(self):
        self.assertEqual(bp.ingest_exit_code(succeeded=0, failed=0), bp.EXIT_OK)

    def test_codes_are_distinct(self):
        codes = {bp.EXIT_OK, bp.EXIT_USAGE, bp.EXIT_PARTIAL, bp.EXIT_FAILED}
        self.assertEqual(len(codes), 4)


class AddMediaExitCode(unittest.TestCase):
    """End-to-end: the real entry point, through real image decoding."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "in"
        self.src.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        import add_media
        argv = ["add_media.py", "--in", str(self.src),
                "--data-dir", str(self.dir / "data")]
        with mock.patch.object(sys, "argv", argv):
            return add_media.main()

    def test_all_inputs_unusable_exits_failed(self):
        (self.src / "broken.png").write_text("not a png", encoding="utf-8")
        (self.src / "also.webp").write_text("garbage", encoding="utf-8")
        self.assertEqual(self._run(), bp.EXIT_FAILED)

    def test_some_inputs_usable_exits_partial(self):
        (self.src / "broken.png").write_text("not a png", encoding="utf-8")
        _png(self.src / "good.png", (0, 180, 90, 255))
        self.assertEqual(self._run(), bp.EXIT_PARTIAL)

    def test_all_inputs_usable_exits_ok(self):
        _png(self.src / "good.png", (0, 180, 90, 255))
        self.assertEqual(self._run(), bp.EXIT_OK)


class LinksDestination(unittest.TestCase):
    """Finished-pack links must go where PACK_LINKS_CHAT_ID says."""

    OWNER = 111111111

    def _env(self, value):
        return mock.patch.dict("os.environ", {"PACK_LINKS_CHAT_ID": value},
                               clear=False)

    def test_unset_falls_back_to_the_owner(self):
        with self._env(""):
            self.assertEqual(bp.links_chat_id(self.OWNER), self.OWNER)

    def test_numeric_channel_id_is_used_as_an_int(self):
        with self._env("-1001111111111"):
            self.assertEqual(bp.links_chat_id(self.OWNER), -1001111111111)

    def test_at_username_is_passed_through(self):
        with self._env("@packlinks"):
            self.assertEqual(bp.links_chat_id(self.OWNER), "@packlinks")

    def test_surrounding_whitespace_is_tolerated(self):
        with self._env("  -1001111111111  "):
            self.assertEqual(bp.links_chat_id(self.OWNER), -1001111111111)

    def test_every_publisher_resolves_the_same_destination(self):
        """A publisher that still hardcoded the owner would fail here."""
        import build_collection
        import coins.rebuild_dedup as rd
        for module in (bp, build_collection, rd):
            self.assertIs(module.links_chat_id, bp.links_chat_id,
                          f"{module.__name__} must use the shared resolver")


class SafeConfigParsing(unittest.TestCase):
    """A typo in .env must not kill the process before argparse can speak."""

    def _env(self, value):
        return mock.patch.dict("os.environ", {"X_TEST_NUM": value}, clear=False)

    def test_valid_value(self):
        with self._env("42"):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 7), 42)

    def test_garbage_falls_back_instead_of_raising(self):
        with self._env("not-a-number"):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 7), 7)

    def test_absent_and_blank_use_the_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 7), 7)
        with self._env("   "):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 7), 7)

    def test_values_are_clamped(self):
        with self._env("999"):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 1, maximum=64), 64)
        with self._env("-5"):
            self.assertEqual(bp.safe_int_env("X_TEST_NUM", 1, minimum=0), 0)


class PublisherLock(unittest.TestCase):
    """Two publishers must not mutate one pack family at the same time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "state.json.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_holder_is_refused(self):
        with bp.exclusive_lock(self.lock):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock):
                    self.fail("a second publisher acquired the lock")

    def test_lock_is_released_on_exit(self):
        with bp.exclusive_lock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())

    def test_lock_is_released_even_on_error(self):
        with self.assertRaises(ZeroDivisionError):
            with bp.exclusive_lock(self.lock):
                1 / 0
        self.assertFalse(self.lock.exists())

    def test_stale_lock_is_reclaimed(self):
        self.lock.write_text("pid=999 (crashed)", encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with bp.exclusive_lock(self.lock, stale_after=3600):
            pass          # must not raise

    def _make_stale(self) -> None:
        self.lock.write_text(json.dumps({"token": "gone", "pid": 999999999,
                                         "started": "2020-01-01 00:00:00 UTC"}),
                             encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))

    def test_a_reclaiming_run_never_deletes_a_fresh_claim(self):
        """Two runs judge the SAME dead record stale; only one may end up holding it.

        The window is between deciding "the holder is gone" and acting on that
        decision. An unconditional unlink there deletes whatever is on disk NOW,
        including the lock a faster run has just claimed -- so the recovery path
        itself hands the pack family to two live writers.

        The other run's claim is planted inside the liveness check, which is the
        last thing that happens before the old code would have unlinked.
        """
        self._make_stale()
        real_alive = bp._lock_owner_is_alive
        planted = {"done": False}

        def alive(pid):
            gone = real_alive(pid)              # the recorded pid really is gone
            if not planted["done"]:
                planted["done"] = True
                self.lock.write_text('{"token": "other", "pid": 1}',
                                     encoding="utf-8")
            return gone

        with mock.patch.object(bp, "_lock_owner_is_alive", alive):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=3600):
                    self.fail("took a lock another run was already holding")
        self.assertIn("other", self.lock.read_text(encoding="utf-8"),
                      "the other run's claim was deleted")

    def test_the_loser_of_a_reclaim_race_gets_the_ordinary_busy_answer(self):
        """Both delete before either creates: O_EXCL decides, the loser backs off.

        The loser used to take an uncaught FileExistsError out of the reclaim's
        second _claim(), so callers that handle LockBusy saw a bare OSError from
        a path that is simply "someone else got there first".
        """
        self._make_stale()
        real_open = bp.os.open
        calls = {"n": 0}

        def racing_open(path, flags, *a, **kw):
            calls["n"] += 1
            # Call 1 is the opening claim (fails: the stale lock is still there).
            # Call 2 is the reclaim's claim -- the other run wins it by a hair.
            if calls["n"] == 2 and Path(path) == self.lock:
                self.lock.write_text('{"token": "other", "pid": 1}',
                                     encoding="utf-8")
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(bp.os, "open", racing_open):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=3600):
                    self.fail("claimed a lock another run had just taken")


class TriStateLiveReads(unittest.TestCase):
    """"Unknown" must never be reported as "the set is empty"."""

    def _tg(self, probe_result):
        tg = bp.Telegram("1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        tg.probe_sticker_set = lambda name: probe_result
        return tg

    def test_exists(self):
        tg = self._tg((True, {"stickers": [{}, {}]}))
        self.assertEqual(tg.probe_set_state("s")[0], bp.SetState.EXISTS)
        self.assertEqual(tg.live_count_strict("s"), 2)

    def test_missing_is_a_real_zero(self):
        tg = self._tg((True, None))
        self.assertEqual(tg.probe_set_state("s")[0], bp.SetState.MISSING)
        self.assertEqual(tg.live_count_strict("s"), 0)

    def test_unknown_raises_instead_of_returning_zero(self):
        tg = self._tg((False, None))
        self.assertEqual(tg.probe_set_state("s")[0], bp.SetState.UNKNOWN)
        with self.assertRaises(bp.LiveStateUnknown):
            tg.live_count_strict("s")


class UnresolvedMutationStopsTheRun(unittest.TestCase):
    """After an ambiguous upload, NO further mutation may be attempted.

    Continuing would overwrite the in-flight intent with the next item's, which
    destroys the only record of which mutation is unresolved.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        for name, color in (("a.png", (200, 0, 0, 255)),
                            ("b.png", (0, 200, 0, 255)),
                            ("c.png", (0, 0, 200, 255))):
            _png(self.src / name, color)
        self.state = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, tg):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def _tg(self):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.return_value = (bp.SetState.MISSING, None)
        tg.send_message.return_value = None
        return tg

    def test_ambiguous_add_stops_before_the_next_item(self):
        tg = self._tg()
        # First item creates the set; the second add comes back ambiguous.
        tg.create_set.return_value = None
        tg.add_sticker.side_effect = bp.AmbiguousUploadError("timed out after apply")
        tg.probe_set_state.return_value = (bp.SetState.EXISTS,
                                           {"stickers": [{"file_unique_id": "f0"}]})

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL, "an unresolved add must not exit 0")
        self.assertEqual(tg.add_sticker.call_count, 1,
                         "no second mutation may be attempted")

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        intent = saved["in_flight"]
        self.assertIsNotNone(intent, "the unresolved intent must be preserved")
        self.assertEqual(intent["operation"], "add")
        self.assertEqual(intent["key"], "b")

    def test_ambiguous_create_records_the_target_set(self):
        tg = self._tg()
        tg.create_set.side_effect = bp.AmbiguousUploadError("timed out after apply")
        tg.probe_set_state.return_value = (bp.SetState.MISSING, None)

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        intent = json.loads(self.state.read_text(encoding="utf-8"))["in_flight"]
        self.assertEqual(intent["operation"], "create")
        self.assertTrue(intent["set_name"],
                        "an ambiguous create must record the set name it targeted")

    def test_unknown_live_state_on_resume_keeps_the_intent(self):
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": {"key": "a", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 0},
        })
        tg = self._tg()
        tg.probe_set_state.return_value = (bp.SetState.UNKNOWN, None)

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()
        intent = json.loads(self.state.read_text(encoding="utf-8"))["in_flight"]
        self.assertIsNotNone(intent, "an unknown probe must not clear the intent")

    def test_state_for_a_different_base_is_refused(self):
        bp.write_json_atomic(self.state, {
            "base": "OTHER", "per_set": 200, "done": [], "sent": [], "sets": [],
        })
        tg = self._tg()
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        tg.create_set.assert_not_called()

    def test_unusable_images_make_the_run_partial(self):
        (self.src / "bad.png").write_bytes(b"")
        tg = self._tg()
        tg.create_set.return_value = None
        tg.add_sticker.return_value = None
        tg.probe_set_state.return_value = (bp.SetState.EXISTS, {"stickers": []})
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL,
                         "a skipped image must not report full success")


class AmbiguousCreateForLaterSets(unittest.TestCase):
    """An ambiguous CREATE of set #2+ must be reconciled against ITS OWN set.

    Recovery used to probe only sets[-1]. For a create, the new set is not in
    state["sets"] yet, so the old last set says nothing about whether it landed
    -- the intent was effectively ignored whenever any earlier set existed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "a.png", (200, 0, 0, 255))
        _png(self.src / "b.png", (0, 200, 0, 255))
        self.state = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, **over):
        base = {
            "base": "t", "per_set": 1, "done": ["a"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 1, "index": 1}],
            "in_flight": {"key": "b", "operation": "create",
                          "set_name": "t2_by_bot", "set_index": 2,
                          "expected_before": 0, "title": "T 2"},
        }
        base.update(over)
        bp.write_json_atomic(self.state, base)

    def _run(self, tg):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state), "--per-set", "1"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def _tg(self, states, *, matches=True):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.side_effect = lambda name: states[name]
        tg.send_message.return_value = None
        # Reconciliation now requires CONTENT proof, so the fake must say
        # whether the live sticker is the image the intent was carrying.
        tg._sticker_matches.return_value = matches
        return tg

    def test_landed_create_of_set_two_is_adopted(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f2"}]}),
        })
        self._run(tg)
        tg.create_set.assert_not_called()
        tg.add_sticker.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "the landed create must be adopted")
        self.assertIn("t2_by_bot", [s["name"] for s in saved["sets"]])
        self.assertIsNone(saved["in_flight"])

    def test_missing_create_of_set_two_leaves_the_item_pending(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.MISSING, None),
        })
        tg.create_set.return_value = None
        self._run(tg)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "b should be retried and then land")
        self.assertTrue(tg.create_set.called, "a MISSING create must be retried")

    def test_a_same_named_set_holding_a_FOREIGN_image_is_never_adopted(self):
        """Existence is not proof we created it.

        A set with the expected name may be someone else's, or left over. If
        its first sticker is not the image the intent was carrying, adopting it
        attaches this run's state to a pack it did not build.
        """
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "foreign"}]}),
        }, matches=False)
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("t2_by_bot", [s["name"] for s in saved["sets"]],
                         "a foreign set must not be adopted")

    def test_a_create_target_holding_EXTRA_stickers_is_never_adopted(self):
        """A create leaves exactly ONE sticker.

        A matching first sticker in a set of several proves only that our image
        is in there somewhere; the set was not left by our interrupted create
        alone. Adopting it books whatever else is in there as this run's work,
        and the recorded count then drives every later expectation.
        """
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f2"},
                                                            {"file_unique_id": "extra"}]}),
        }, matches=True)          # even a matching FIRST sticker is not enough
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        tg.create_set.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("t2_by_bot", [s["name"] for s in saved["sets"]],
                         "a set we may not have created must not be adopted")
        self.assertNotIn("b", saved["done"])

    def test_an_unverifiable_create_target_stops_retryably(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "x"}]}),
        }, matches=None)
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)

    def test_unknown_create_stops_retryably(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (bp.SetState.UNKNOWN, None),
        })
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)
        tg.create_set.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNotNone(saved["in_flight"], "UNKNOWN must keep the intent")


class RecordedSetIntegrity(unittest.TestCase):
    """A recorded set that vanished or shrank invalidates the whole resume."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "a.png", (200, 0, 0, 255))
        self.state = self.dir / "state.json"
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": ["x"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 5, "index": 1}],
            "in_flight": None,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, probe):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.return_value = probe
        tg.send_message.return_value = None
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main(), tg

    def test_deleted_recorded_set_is_an_integrity_stop(self):
        code, tg = self._run((bp.SetState.MISSING, None))
        self.assertEqual(code, bp.EXIT_FAILED)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_shrunken_set_is_an_integrity_stop(self):
        code, tg = self._run(
            (bp.SetState.EXISTS, {"stickers": [{"file_unique_id": "f"}] * 3}))
        self.assertEqual(code, bp.EXIT_FAILED, "negative drift must not be ignored")
        tg.add_sticker.assert_not_called()

    def test_unknown_recorded_set_is_retryable(self):
        code, tg = self._run((bp.SetState.UNKNOWN, None))
        self.assertEqual(code, bp.EXIT_PARTIAL)
        tg.add_sticker.assert_not_called()


class StateShapeValidation(unittest.TestCase):
    """Valid JSON is not valid state; every resume number is load-bearing."""

    def _ok(self, **over):
        s = {"base": "t", "per_set": 200, "done": ["a"], "sets": [
            {"name": "t1_by_bot", "title": "T 1", "count": 3, "index": 1}],
            "in_flight": None}
        s.update(over)
        return s

    def test_a_sound_state_passes(self):
        bp.validate_state_shape(self._ok(), base="t", per_set=200)

    def test_wrong_base_is_rejected(self):
        with self.assertRaises(bp.StateInvalid):
            bp.validate_state_shape(self._ok(base="other"), base="t", per_set=200)

    def test_negative_and_oversized_counts_are_rejected(self):
        for bad in (-1, 201):
            s = self._ok(sets=[{"name": "n", "title": "", "count": bad, "index": 1}])
            with self.assertRaises(bp.StateInvalid):
                bp.validate_state_shape(s, base="t", per_set=200)

    def test_duplicate_and_backwards_indexes_are_rejected(self):
        dup = self._ok(sets=[{"name": "a", "title": "", "count": 1, "index": 1},
                             {"name": "b", "title": "", "count": 1, "index": 1}])
        back = self._ok(sets=[{"name": "a", "title": "", "count": 1, "index": 2},
                              {"name": "b", "title": "", "count": 1, "index": 1}])
        for s in (dup, back):
            with self.assertRaises(bp.StateInvalid):
                bp.validate_state_shape(s, base="t", per_set=200)

    def test_malformed_intent_is_rejected(self):
        for bad in ({"key": "a"}, {"key": "a", "operation": "add"},
                    {"key": "a", "operation": "wat", "set_name": "s"}):
            with self.assertRaises(bp.StateInvalid):
                bp.validate_state_shape(self._ok(in_flight=bad),
                                        base="t", per_set=200)

    def test_done_must_hold_strings(self):
        with self.assertRaises(bp.StateInvalid):
            bp.validate_state_shape(self._ok(done=[1, 2]), base="t", per_set=200)


class LockOwnership(unittest.TestCase):
    """A lock may only be removed by the process that still owns it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "pack_x.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_live_holder_is_never_reclaimed_however_old(self):
        with bp.exclusive_lock(self.lock):
            old = time.time() - 10 * 24 * 3600
            os.utime(self.lock, (old, old))       # ancient, but WE are alive
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=1):
                    self.fail("stole a lock from a live process")

    def test_a_dead_holder_is_reclaimed(self):
        self.lock.write_text(
            json.dumps({"token": "t", "pid": 999_999_999, "started": "old"}),
            encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with bp.exclusive_lock(self.lock, stale_after=3600):
            pass                                   # must not raise

    def test_a_reclaimed_lock_is_not_deleted_by_the_old_holder(self):
        """The bug: the original holder unlinked the REPLACEMENT holder's lock."""
        cm = bp.exclusive_lock(self.lock)
        cm.__enter__()
        # Another process takes over the file entirely.
        self.lock.write_text(json.dumps(
            {"token": "other", "pid": 4242, "started": "now"}), encoding="utf-8")
        cm.__exit__(None, None, None)
        self.assertTrue(self.lock.exists(),
                        "must not remove a lock owned by someone else")

    def test_heartbeat_refreshes_the_lock(self):
        with bp.exclusive_lock(self.lock) as heartbeat:
            old = time.time() - 10_000
            os.utime(self.lock, (old, old))
            heartbeat()
            self.assertGreater(self.lock.stat().st_mtime, old + 1000)


class PackFamilyLock(unittest.TestCase):
    """Every tool touching one pack family must contend for the SAME lock."""

    def test_same_base_yields_the_same_path(self):
        self.assertEqual(bp.pack_family_lock_path("cryptoemoji"),
                         bp.pack_family_lock_path("cryptoemoji"))

    def test_different_bases_do_not_collide(self):
        self.assertNotEqual(bp.pack_family_lock_path("one"),
                            bp.pack_family_lock_path("two"))

    def test_unsafe_characters_are_normalised(self):
        p = bp.pack_family_lock_path("../../etc/passwd")
        self.assertEqual(p.parent, bp.LOCK_DIR)
        self.assertNotIn("..", p.name)


class AddedCheckUsesIdentity(unittest.TestCase):
    """A count increase is not proof that OUR upload landed.

    A second writer adding something unrelated produces the same +1 while our
    request failed. Acting on that marks the wrong item done -- the mechanism
    that put a Solama llama on the `sol` ticker.
    """

    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "ours.png"
        _png(self.src, (200, 30, 30, 255))

    def tearDown(self):
        self.tmp.cleanup()

    def _tg(self, after_fuids, *, matches=True):
        tg = bp.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (
            True, {"stickers": [{"file_unique_id": f} for f in after_fuids]})
        tg._sticker_matches = lambda st, src: matches
        return tg

    def test_our_own_new_sticker_is_recognised(self):
        tg = self._tg(["a", "b", "MINE"], matches=True)
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIs(check(), True)

    def test_nothing_new_means_not_applied(self):
        tg = self._tg(["a", "b"])
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIs(check(), False)

    def test_one_new_but_FOREIGN_sticker_is_not_our_upload(self):
        """The case the previous version of this test got wrong.

        It listed both THEIRS and MINE, which is two new identities and takes
        the easy branch. The dangerous shape is ONE new identity that is not
        ours: our add failed while someone else's landed. Identity alone reads
        that as success, so the content must be compared.
        """
        tg = self._tg(["a", "b", "THEIRS"], matches=False)
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIs(check(), False, "a stranger's sticker is not our upload")

    def test_one_new_sticker_we_cannot_verify_is_unknown(self):
        tg = self._tg(["a", "b", "?"], matches=None)
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIsNone(check())

    def test_two_new_identities_are_unattributable(self):
        tg = self._tg(["a", "b", "THEIRS", "MINE"])
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIsNone(check())

    def test_a_replacement_is_not_read_as_our_add(self):
        # Same count as expected+1 overall, but an old identity vanished too.
        tg = self._tg(["a", "THEIRS", "MINE"])
        check = tg._added_check("s", 2, known_before={"a", "b"}, source=self.src)
        self.assertIsNone(check())

    def test_without_a_snapshot_nothing_is_claimed(self):
        """No count fallback: expected+1 is not evidence of whose sticker it is."""
        tg = self._tg(["a", "b", "x"])
        self.assertIsNone(tg._added_check("s", 2)())

    def test_stickers_without_identity_are_unknown_not_applied(self):
        """Unusable identities must be UNKNOWN in BOTH directions.

        Answering False re-sends an upload that may have landed (a duplicate);
        answering True from the count attributes a stranger's sticker to us.
        Only UNKNOWN is safe, which makes the caller reconcile.
        """
        tg = bp.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (
            True, {"stickers": [{"i": 0}, {"i": 1}]})     # no identities at all
        check = tg._added_check("s", 1, known_before={"None"}, source=self.src)
        self.assertIsNone(check())

    def test_duplicate_identities_are_not_trusted(self):
        self.assertIsNone(bp._usable_fuids(
            [{"file_unique_id": "same"}, {"file_unique_id": "same"}]))
        tg = bp.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, {"stickers": [
            {"file_unique_id": "same"}, {"file_unique_id": "same"}]})
        self.assertIsNone(
            tg._added_check("s", 1, known_before={"x"}, source=self.src)())

    def test_usable_fuids_accepts_a_well_formed_set(self):
        self.assertEqual(
            bp._usable_fuids([{"file_unique_id": "a"}, {"file_unique_id": "b"}]),
            {"a", "b"})

    def test_usable_fuids_rejects_a_missing_id(self):
        self.assertIsNone(
            bp._usable_fuids([{"file_unique_id": "a"}, {"file_unique_id": ""}]))


class CreateAdoptionVerifiesContent(unittest.TestCase):
    """Set existence is not proof that WE created it."""

    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "ours.png"
        _png(self.src, (12, 34, 56, 255))

    def tearDown(self):
        self.tmp.cleanup()

    def _tg(self, stickers, matches):
        tg = bp.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, {"stickers": stickers})
        tg._sticker_matches = lambda st, src: matches
        return tg

    def test_existence_alone_is_not_adopted(self):
        tg = self._tg([{"file_unique_id": "foreign"}], matches=False)
        self.assertIs(tg._created_check("s", expect_first=self.src)(), False)

    def test_our_own_image_is_adopted(self):
        tg = self._tg([{"file_unique_id": "ours"}], matches=True)
        self.assertIs(tg._created_check("s", expect_first=self.src)(), True)

    def test_a_populated_set_is_not_the_shape_we_would_have_left(self):
        tg = self._tg([{"file_unique_id": "a"}, {"file_unique_id": "b"}],
                      matches=True)
        self.assertIsNone(tg._created_check("s", expect_first=self.src)())

    def test_unverifiable_content_stays_unknown(self):
        tg = self._tg([{"file_unique_id": "x"}], matches=None)
        self.assertIsNone(tg._created_check("s", expect_first=self.src)())

    def test_missing_set_is_a_definite_no(self):
        tg = bp.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, None)
        self.assertIs(tg._created_check("s", expect_first=self.src)(), False)


class PosixStaleLockReclaim(unittest.TestCase):
    """A crashed POSIX holder must not own its lock forever."""

    def test_no_such_process_is_reported_dead(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(bp._lock_owner_is_alive(4242))

    def test_permission_denied_means_it_exists(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=PermissionError):
            self.assertTrue(bp._lock_owner_is_alive(4242))

    def test_unknown_failure_stays_conservative(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=OSError("weird")):
            self.assertTrue(bp._lock_owner_is_alive(4242))

    def test_a_running_process_is_alive(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", return_value=None):
            self.assertTrue(bp._lock_owner_is_alive(4242))


class CanonicalMapLock(unittest.TestCase):
    """Every writer of ticker_to_id.json must contend for one lock."""

    def test_all_callers_get_the_same_path(self):
        a, b = bp.canonical_map_lock(), bp.canonical_map_lock()
        self.assertEqual(a.args[0], b.args[0])

    def test_it_actually_excludes(self):
        with bp.canonical_map_lock():
            with self.assertRaises(bp.LockBusy):
                with bp.canonical_map_lock():
                    self.fail("two map writers held the lock at once")

    def test_it_is_not_the_pack_family_lock(self):
        with bp.canonical_map_lock():
            with bp.exclusive_lock(bp.pack_family_lock_path("cryptoemoji")):
                pass          # different concerns must not block each other


class SharedLogoGuard(unittest.TestCase):
    """The detector that would have caught the 129-ticker collision."""

    def setUp(self):
        from coins import rebuild_dedup as rd
        self.rd = rd

    def test_unreviewed_many_to_one_group_is_reported(self):
        mapping = {t: "SAME" for t in ("apt", "arkm", "hbar", "near", "tao")}
        mapping["btc"] = "OWN"
        with mock.patch.object(self.rd, "approved_shared_tickers", return_value=set()):
            bad = self.rd.unapproved_shared_groups(mapping)
        self.assertEqual(list(bad), ["SAME"])
        self.assertEqual(len(bad["SAME"]), 5)

    def test_reviewed_shared_group_is_accepted(self):
        mapping = {"usdt": "T", "usdtbsc": "T", "usdterc20": "T"}
        approved = {"usdt", "usdtbsc", "usdterc20"}
        with mock.patch.object(self.rd, "approved_shared_tickers", return_value=approved):
            self.assertEqual(self.rd.unapproved_shared_groups(mapping), {})

    def test_one_to_one_map_is_clean(self):
        mapping = {"btc": "1", "eth": "2", "sol": "3"}
        with mock.patch.object(self.rd, "approved_shared_tickers", return_value=set()):
            self.assertEqual(self.rd.unapproved_shared_groups(mapping), {})

    def test_the_real_committed_map_is_checked_against_the_real_groups(self):
        """The shipped map must not regain an unreviewed collision."""
        ids = ROOT / "coins" / "ticker_to_id.json"
        if not ids.is_file():
            self.skipTest("no committed ticker map")
        mapping = json.loads(ids.read_text(encoding="utf-8"))
        bad = self.rd.unapproved_shared_groups(mapping)
        biggest = max((len(v) for v in bad.values()), default=0)
        self.assertLess(biggest, 20,
                        f"an emoji id is shared by {biggest} unreviewed tickers; "
                        f"this is the positional-drift signature")


class _Resp:
    """Minimal stand-in for a requests Response."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _ForeignWriterServer:
    """Bot API fake in which the FIRST add lets a stranger's sticker in.

    Our request fails at the network level while Telegram accepts someone
    else's sticker -- exactly the shape ``_added_check`` answers False for, so
    the retry uploads ours and the set ends up TWO bigger than it started.
    """

    def __init__(self, live: int = 1, on_add=None):
        self.stickers = [{"file_unique_id": f"live{i}", "file_id": f"live{i}"}
                         for i in range(live)]
        self.adds = 0
        self.probes = 0
        self.probe_fails_from = None     # nth getStickerSet onwards fails
        self.on_add = on_add

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        if method == "getMe":
            return _Resp({"ok": True, "result": {"username": "bot"}})
        if method == "sendMessage":
            return _Resp({"ok": True, "result": {}})
        if method == "getStickerSet":
            self.probes += 1
            if self.probe_fails_from and self.probes >= self.probe_fails_from:
                raise requests.ConnectionError("probe failed")
            return _Resp({"ok": True, "result": {"stickers": list(self.stickers)}})
        if method == "addStickerToSet":
            self.adds += 1
            if self.on_add is not None:
                self.on_add()
            if self.adds == 1:
                self.stickers.append({"file_unique_id": "foreign",
                                      "file_id": "foreign"})
                raise requests.ReadTimeout("timeout; ours never applied")
            self.stickers.append({"file_unique_id": f"ours{self.adds}",
                                  "file_id": f"ours{self.adds}"})
            return _Resp({"ok": True, "result": True})
        raise AssertionError(f"unexpected method {method}")


class _LaggingReadServer(_ForeignWriterServer):
    """The same foreign writer, but the read AFTER the retry has not caught up.

    Telegram acknowledged our add; the getStickerSet that follows is served
    before our own write is visible in it. The only identity that is new since
    the snapshot is therefore the STRANGER's -- which is exactly what the size
    guard has to refuse, because a foreign sticker landing during the failed
    attempt is the situation this whole path exists for.
    """

    def __init__(self, live: int = 1, stale_from: int = 3):
        # Probe 1 is the pre-add snapshot, 2 the applied-check, 3 the size read.
        super().__init__(live=live)
        self.stale_from = stale_from

    def post(self, url, data=None, files=None, timeout=None):
        if (url.rsplit("/", 1)[-1] == "getStickerSet"
                and self.probes + 1 >= self.stale_from):
            self.probes += 1
            visible = [s for s in self.stickers
                       if not s["file_unique_id"].startswith("ours")]
            return _Resp({"ok": True, "result": {"stickers": visible}})
        return super().post(url, data=data, files=files, timeout=timeout)


class BookkeepingFollowsLiveState(unittest.TestCase):
    """A retried add can move the set by TWO, not one.

    The applied-check correctly refuses a foreign sticker and the retry uploads
    ours, so the set grows twice while the run books a single add. Every later
    ``expected_before`` is derived from that number, so from then on the state
    describes a set that no longer exists.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "b.png", (200, 0, 0, 255))
        _png(self.src / "c.png", (0, 200, 0, 255))
        self.state = self.dir / "state.json"
        bp.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": ["a"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 1, "index": 1}],
            "in_flight": None,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _tg(self, server):
        tg = bp.Telegram("TESTTOKEN")
        tg.s = server
        # Only the stranger's sticker fails the content comparison; the real
        # comparison downloads and decodes, which is covered elsewhere.
        tg._sticker_matches = lambda st, src: st["file_unique_id"].startswith("ours")
        return tg

    def _run(self, server):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=self._tg(server)), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def test_a_foreign_sticker_during_a_retry_does_not_drift_the_count(self):
        intents = []
        srv = _ForeignWriterServer(live=1, on_add=lambda: intents.append(
            json.loads(self.state.read_text(encoding="utf-8"))["in_flight"]))
        self.assertEqual(self._run(srv), bp.EXIT_OK)

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["sets"][0]["count"], len(srv.stickers),
                         "the recorded count must describe the live set")
        # b was retried past a foreign sticker (1 live + foreign + ours = 3),
        # so c must be announced against 3 -- not the assumed 2.
        self.assertEqual([i["expected_before"] for i in intents], [1, 1, 3])
        self.assertEqual([i["key"] for i in intents], ["b", "b", "c"])
        self.assertEqual(sorted(saved["done"]), ["a", "b", "c"])

    def test_an_unknown_size_after_a_retried_add_is_not_guessed(self):
        """The add landed, so it must not be re-sent -- and must not be booked
        at a guessed size either: the caller keeps the intent and reconciles."""
        srv = _ForeignWriterServer(live=1)
        srv.probe_fails_from = 3          # the size probe after the retry
        tg = self._tg(srv)
        with self.assertRaises(bp.AmbiguousUploadError):
            tg.add_sticker(1, "t1_by_bot", self.src / "b.png",
                           bp.DEFAULT_EMOJI, "kw", expected_before=1)
        self.assertEqual(srv.adds, 2, "the add landed; it must not be re-sent")

    def test_a_foreign_sticker_alone_does_not_certify_the_size(self):
        """The guard must prove OURS is there, not that SOMETHING is.

        It only ever required an identity the snapshot did not have -- which a
        stranger's sticker satisfies, and a stranger's sticker landing during
        the failed attempt is the very case this path exists for. So the guard
        passed in precisely the situation it was written to catch, and a size
        read off a set that does not contain our sticker was booked as the
        result of our upload; every later expected_before came from it.
        """
        srv = _LaggingReadServer(live=1)
        tg = self._tg(srv)
        with mock.patch.object(bp.time, "sleep", lambda s: None):
            with self.assertRaises(bp.AmbiguousUploadError):
                tg.add_sticker(1, "t1_by_bot", self.src / "b.png",
                               bp.DEFAULT_EMOJI, "kw", expected_before=1)
        self.assertEqual(srv.adds, 2, "the add landed; it must not be re-sent")
        self.assertEqual(len(srv.stickers), 3,
                         "ours really is live -- only the read back lagged, "
                         "which is why a guessed size must not be booked")

    def test_an_undisturbed_add_costs_no_extra_probe(self):
        """The fix must not add a round trip per sticker."""
        srv = _ForeignWriterServer(live=1)
        srv.adds = 1                      # skip the foreign-writer attempt
        tg = self._tg(srv)
        live = tg.add_sticker(1, "t1_by_bot", self.src / "b.png",
                              bp.DEFAULT_EMOJI, "kw", expected_before=1)
        self.assertIsNone(live, "a clean add stays at the assumed +1")
        self.assertEqual(srv.probes, 1, "only the pre-add identity snapshot")


if __name__ == "__main__":
    unittest.main()
