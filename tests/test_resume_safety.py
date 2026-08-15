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
import sys
import tempfile
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

    def _fake_tg(self, live_count: int):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_sticker_set.return_value = (
            True, {"stickers": [{"file_unique_id": f"f{i}"} for i in range(live_count)]})
        tg.add_sticker.return_value = None
        tg.create_set.return_value = None
        tg.send_message.return_value = None
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
        self.assertEqual(self._run(tg), 0)

        uploaded = [c.args[2].stem for c in tg.add_sticker.call_args_list]
        uploaded += [c.args[3].stem for c in tg.create_set.call_args_list]
        self.assertNotIn("b", uploaded, "b.png was already live and must not re-upload")
        self.assertIn("c", uploaded, "c.png is genuinely pending")

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"])
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
        self.assertEqual(self._run(tg), 4, "must refuse rather than mis-attribute")
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_corrupt_state_does_not_restart_from_zero(self):
        self.state.write_text('{"base": "t", "done": [trunca', encoding="utf-8")
        tg = self._fake_tg(live_count=0)
        self.assertEqual(self._run(tg), 4)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_intent_is_recorded_before_each_upload(self):
        tg = self._fake_tg(live_count=0)
        seen = []

        def record(*a, **kw):
            seen.append(json.loads(self.state.read_text(encoding="utf-8"))["in_flight"])

        tg.create_set.side_effect = record
        tg.add_sticker.side_effect = record
        self.assertEqual(self._run(tg), 0)
        # a.png is unusable and skipped; b and c upload, each announced first.
        self.assertEqual(seen, ["b", "c"])


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


if __name__ == "__main__":
    unittest.main()
