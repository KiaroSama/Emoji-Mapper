"""Regression tests for the resume/duplicate-upload defects.

These lock down the paths that actually put the same emoji in a pack twice:

* a skipped source image shifting the resume cursor (build_pack + rebuild_dedup),
* a truncated JSON state file being read back as "nothing uploaded yet",
* an ambiguous mutation followed by another one, which destroys the only record
  of which upload is unresolved,
* a set of the right NAME adopted as one we created.

Everything here drives `build_pack.main()` over a state file. The client that
those runs call is covered by `test_telegram_client`, the locks they take by
`test_pack_locks`.

No network and no real sleeps: every Telegram call is faked.
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

import build_pack as bp  # noqa: E402
from emojikit import telegram_api as tg_api  # noqa: E402
from emojikit import packstate as ps  # noqa: E402
from emojikit import announce  # noqa: E402
from tests._pack_fixtures import _png  # noqa: E402


class AtomicJsonWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_and_leaves_no_temp_file(self):
        target = self.dir / "state.json"
        ps.write_json_atomic(target, {"done": ["a", "b"]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"done": ["a", "b"]})
        self.assertEqual(list(self.dir.iterdir()), [target])

    def test_previous_content_survives_a_failed_write(self):
        target = self.dir / "state.json"
        ps.write_json_atomic(target, {"done": ["a"]})
        # A crash mid-serialisation must not truncate the existing file.
        unserialisable = {"done": {1, 2}}
        with self.assertRaises(TypeError):
            ps.write_json_atomic(target, unserialisable)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"done": ["a"]})

    def test_overwrites_in_place(self):
        target = self.dir / "state.json"
        ps.write_json_atomic(target, {"n": 1})
        ps.write_json_atomic(target, {"n": 2})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"n": 2})


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
        self.assertEqual(tg_api.PER_SET, 200)
        self.assertEqual(tg_api.MAX_PER_SET, 200)

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
        tg.probe_set_state.return_value = (tg_api.SetState.EXISTS, sset)
        tg.add_sticker.return_value = None
        tg.create_set.return_value = None
        tg.send_message.return_value = None
        if isinstance(matches, dict):
            # Keyed by file_unique_id: the ONLY form that can prove WHICH
            # sticker was compared against the source. `return_value` is
            # argument-insensitive and the list form is merely call-order
            # sensitive, so both answer "did it ask?" rather than "did it ask
            # about the right one?" -- which is the whole question here.
            tg._sticker_matches.side_effect = (
                lambda st, src: matches.get(str(st.get("file_unique_id"))))
        elif isinstance(matches, list):
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
        ps.write_json_atomic(self.state, {
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
        ps.write_json_atomic(self.state, {
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
        ps.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": {"key": "b", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 0},
        })
        # Two stickers arrived; only the SECOND is ours -- keyed by identity, so
        # the assertion is about WHICH sticker matched, not merely that one did.
        tg = self._fake_tg(live_count=2, matches={"f0": False, "f1": True})
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
        ps.write_json_atomic(self.state, {
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
        ps.write_json_atomic(self.state, {
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
            self.assertEqual(announce.links_chat_id(self.OWNER), self.OWNER)

    def test_numeric_channel_id_is_used_as_an_int(self):
        with self._env("-1001111111111"):
            self.assertEqual(announce.links_chat_id(self.OWNER), -1001111111111)

    def test_at_username_is_passed_through(self):
        with self._env("@packlinks"):
            self.assertEqual(announce.links_chat_id(self.OWNER), "@packlinks")

    def test_surrounding_whitespace_is_tolerated(self):
        with self._env("  -1001111111111  "):
            self.assertEqual(announce.links_chat_id(self.OWNER), -1001111111111)

    def test_every_publisher_resolves_the_same_destination(self):
        """A publisher that still hardcoded the owner would fail here.

        The shared thing is now ``announce_packs``, which resolves the
        destination itself -- a stronger guarantee than sharing the resolver,
        because a publisher can no longer call it and then send somewhere else.
        The two collector-side modules deliberately do not import
        ``links_chat_id`` any more: what they cannot reach, they cannot misuse.
        """
        import build_collection
        import coins.rebuild_dedup as rd
        for module in (bp, build_collection, rd):
            self.assertIs(module.announce_packs, announce.announce_packs,
                          f"{module.__name__} must use the shared announcer")
        self.assertFalse(hasattr(build_collection, "links_chat_id"))
        self.assertFalse(hasattr(rd, "links_chat_id"))


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


class StateShapeValidation(unittest.TestCase):
    """Valid JSON is not valid state; every resume number is load-bearing."""

    def _ok(self, **over):
        s = {"base": "t", "per_set": 200, "done": ["a"], "sets": [
            {"name": "t1_by_bot", "title": "T 1", "count": 3, "index": 1}],
            "in_flight": None}
        s.update(over)
        return s

    def test_a_sound_state_passes(self):
        ps.validate_state_shape(self._ok(), base="t", per_set=200)

    def test_wrong_base_is_rejected(self):
        with self.assertRaises(ps.StateInvalid):
            ps.validate_state_shape(self._ok(base="other"), base="t", per_set=200)

    def test_negative_and_oversized_counts_are_rejected(self):
        for bad in (-1, 201):
            s = self._ok(sets=[{"name": "n", "title": "", "count": bad, "index": 1}])
            with self.assertRaises(ps.StateInvalid):
                ps.validate_state_shape(s, base="t", per_set=200)

    def test_duplicate_and_backwards_indexes_are_rejected(self):
        dup = self._ok(sets=[{"name": "a", "title": "", "count": 1, "index": 1},
                             {"name": "b", "title": "", "count": 1, "index": 1}])
        back = self._ok(sets=[{"name": "a", "title": "", "count": 1, "index": 2},
                              {"name": "b", "title": "", "count": 1, "index": 1}])
        for s in (dup, back):
            with self.assertRaises(ps.StateInvalid):
                ps.validate_state_shape(s, base="t", per_set=200)

    def test_malformed_intent_is_rejected(self):
        for bad in ({"key": "a"}, {"key": "a", "operation": "add"},
                    {"key": "a", "operation": "wat", "set_name": "s"}):
            with self.assertRaises(ps.StateInvalid):
                ps.validate_state_shape(self._ok(in_flight=bad),
                                        base="t", per_set=200)

    def test_done_must_hold_strings(self):
        with self.assertRaises(ps.StateInvalid):
            ps.validate_state_shape(self._ok(done=[1, 2]), base="t", per_set=200)


if __name__ == "__main__":
    unittest.main()
