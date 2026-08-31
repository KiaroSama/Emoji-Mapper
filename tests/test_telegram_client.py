"""The Bot API client in build_pack: transport safety and upload evidence.

Split out of `test_resume_safety` because the subject is `build_pack.Telegram`
itself rather than the run that drives it. Two themes, one component:

* transport -- the bot token never reaches stdout through an exception message,
  and STICKERSET_INVALID is not retried for six minutes on a plain lookup;
* evidence -- what the client will accept as proof that a mutation landed.
  A count increase is not proof; only the CONTENT of the arrivals is, and
  "I could not look" must stay UNKNOWN rather than collapse into no.

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

import build_pack as bp  # noqa: E402
import telegram_api as tg_api  # noqa: E402
import packstate as ps  # noqa: E402
from tests._pack_fixtures import _png  # noqa: E402


class TokenRedaction(unittest.TestCase):
    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_exception_text_never_carries_the_token(self):
        tg = tg_api.Telegram(self.TOKEN)
        exc = requests.ConnectionError(
            f"HTTPSConnectionPool: /bot{self.TOKEN}/addStickerToSet failed")
        out = tg._safe(exc)
        self.assertNotIn(self.TOKEN, out)
        self.assertIn("[REDACTED]", out)

    def test_network_retry_output_is_redacted(self):
        tg = tg_api.Telegram(self.TOKEN)
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
        tg = tg_api.Telegram(self.TOKEN)
        slept = []
        with mock.patch.object(tg.s, "post", return_value=self._session()), \
             mock.patch.object(bp.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                tg._call("getStickerSet", data={"name": "nope"})
        self.assertEqual(slept, [], "a plain lookup must not sleep on a missing set")

    def test_create_still_waits_for_the_name_lock(self):
        tg = tg_api.Telegram(self.TOKEN)
        slept = []
        with mock.patch.object(tg.s, "post", return_value=self._session()), \
             mock.patch.object(bp.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                tg._call("createNewStickerSet", data={"name": "x"}, retries=3)
        self.assertTrue(slept, "a create must still wait for a released name")
        self.assertLessEqual(sum(slept), tg_api.NAME_LOCK_TIMEOUT,
                             "waiting must be bounded by the deadline")
        self.assertEqual(len(slept), 2, "no sleep after the final attempt")


class TriStateLiveReads(unittest.TestCase):
    """"Unknown" must never be reported as "the set is empty"."""

    def _tg(self, probe_result):
        tg = tg_api.Telegram("1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        tg.probe_sticker_set = lambda name: probe_result
        return tg

    def test_exists(self):
        tg = self._tg((True, {"stickers": [{}, {}]}))
        self.assertEqual(tg.probe_set_state("s")[0], tg_api.SetState.EXISTS)
        self.assertEqual(tg.live_count_strict("s"), 2)

    def test_missing_is_a_real_zero(self):
        tg = self._tg((True, None))
        self.assertEqual(tg.probe_set_state("s")[0], tg_api.SetState.MISSING)
        self.assertEqual(tg.live_count_strict("s"), 0)

    def test_unknown_raises_instead_of_returning_zero(self):
        tg = self._tg((False, None))
        self.assertEqual(tg.probe_set_state("s")[0], tg_api.SetState.UNKNOWN)
        with self.assertRaises(tg_api.LiveStateUnknown):
            tg.live_count_strict("s")


class PostAddSizeIsMeasured(unittest.TestCase):
    """`_live_after_add` books a size only if the read-back holds OUR sticker.

    It runs when a retried add succeeded, so the set may have grown by two: the
    failed attempt let a foreign sticker in, then our retry landed. Booking a
    bare `len()` there is what put every later `expected_before` out by one, and
    a MISSING set read as ZERO made the run invent a second pack and exit 0.
    A mutation test showed nothing covered the requirement.
    """

    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def _tg(self, stickers, *, matches):
        tg = tg_api.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, {"stickers": stickers})
        tg._sticker_matches = lambda st, src: matches.get(
            str(st.get("file_unique_id")))
        return tg

    def _fired_check(self, tg, known_before):
        check = tg._added_check("s1", 1, known_before=known_before,
                                source=Path("whatever.png"))
        check.fired = True          # a live probe was needed: the retry path
        return check

    def test_a_foreign_arrival_alone_does_not_book_a_size(self):
        live = [{"file_unique_id": "old"}, {"file_unique_id": "THEIRS"}]
        tg = self._tg(live, matches={"THEIRS": False})
        with self.assertRaises(tg_api.AmbiguousUploadError):
            tg._live_after_add("s1", self._fired_check(tg, {"old"}))

    def test_an_unverifiable_arrival_does_not_book_a_size(self):
        live = [{"file_unique_id": "old"}, {"file_unique_id": "UNREADABLE"}]
        tg = self._tg(live, matches={"UNREADABLE": None})
        with self.assertRaises(tg_api.AmbiguousUploadError):
            tg._live_after_add("s1", self._fired_check(tg, {"old"}))

    def test_our_sticker_present_books_the_true_size(self):
        """Positive control, and the case the whole path exists for: the set
        grew by TWO -- a stranger's sticker, then our retry."""
        live = [{"file_unique_id": "old"}, {"file_unique_id": "THEIRS"},
                {"file_unique_id": "OURS"}]
        tg = self._tg(live, matches={"THEIRS": False, "OURS": True})
        self.assertEqual(tg._live_after_add("s1", self._fired_check(tg, {"old"})), 3)

    def test_a_missing_set_is_never_booked_as_zero(self):
        tg = tg_api.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, None)      # MISSING
        check = tg._added_check("s1", 1, known_before={"old"},
                                source=Path("whatever.png"))
        check.fired = True
        with self.assertRaises(tg_api.AmbiguousUploadError):
            tg._live_after_add("s1", check)


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
        tg = tg_api.Telegram(self.TOKEN)
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
        tg = tg_api.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (
            True, {"stickers": [{"i": 0}, {"i": 1}]})     # no identities at all
        check = tg._added_check("s", 1, known_before={"None"}, source=self.src)
        self.assertIsNone(check())

    def test_duplicate_identities_are_not_trusted(self):
        self.assertIsNone(tg_api._usable_fuids(
            [{"file_unique_id": "same"}, {"file_unique_id": "same"}]))
        tg = tg_api.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, {"stickers": [
            {"file_unique_id": "same"}, {"file_unique_id": "same"}]})
        self.assertIsNone(
            tg._added_check("s", 1, known_before={"x"}, source=self.src)())

    def test_usable_fuids_accepts_a_well_formed_set(self):
        self.assertEqual(
            tg_api._usable_fuids([{"file_unique_id": "a"}, {"file_unique_id": "b"}]),
            {"a", "b"})

    def test_usable_fuids_rejects_a_missing_id(self):
        self.assertIsNone(
            tg_api._usable_fuids([{"file_unique_id": "a"}, {"file_unique_id": ""}]))


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
        tg = tg_api.Telegram(self.TOKEN)
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
        tg = tg_api.Telegram(self.TOKEN)
        tg.probe_sticker_set = lambda name: (True, None)
        self.assertIs(tg._created_check("s", expect_first=self.src)(), False)


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
        ps.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": ["a"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 1, "index": 1}],
            "in_flight": None,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _tg(self, server):
        tg = tg_api.Telegram("TESTTOKEN")
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
        with self.assertRaises(tg_api.AmbiguousUploadError):
            tg.add_sticker(1, "t1_by_bot", self.src / "b.png",
                           tg_api.DEFAULT_EMOJI, "kw", expected_before=1)
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
            with self.assertRaises(tg_api.AmbiguousUploadError):
                tg.add_sticker(1, "t1_by_bot", self.src / "b.png",
                               tg_api.DEFAULT_EMOJI, "kw", expected_before=1)
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
                              tg_api.DEFAULT_EMOJI, "kw", expected_before=1)
        self.assertIsNone(live, "a clean add stays at the assumed +1")
        self.assertEqual(srv.probes, 1, "only the pre-add identity snapshot")


if __name__ == "__main__":
    unittest.main()


class WaitsReachTheLogFile(unittest.TestCase):
    """Every pause this client takes must leave a trace in the run's log.

    `build_pack` had no logger at all, so flood waits, name-lock waits and
    network retries were printed to the console and nowhere else. A publish
    that stalled 269 s on a flood wait wrote NOTHING to its log for the whole
    pause -- which reads exactly like a hung process, and leaves nothing to
    read afterwards. Every tool in the project shares this client, so they were
    all blind together.
    """

    TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_a_flood_wait_is_logged_not_only_printed(self):
        tg = tg_api.Telegram(self.TOKEN)
        busy, ok = mock.Mock(), mock.Mock()
        busy.json.return_value = {
            "ok": False, "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7}}
        ok.json.return_value = {"ok": True, "result": {"done": True}}
        with mock.patch.object(tg.s, "post", side_effect=[busy, ok]), \
             mock.patch.object(bp.time, "sleep", lambda s: None), \
             self.assertLogs("build_pack", level="WARNING") as caught:
            self.assertEqual(tg._call("addStickerToSet"), {"done": True})
        line = "\n".join(caught.output)
        self.assertIn("flood wait", line)
        self.assertIn("7", line, "the log must say HOW LONG the pause is")
        self.assertIn("addStickerToSet", line, "and which call is waiting")

    def test_a_network_retry_is_logged_with_the_token_redacted(self):
        """New log output must not become a new way to leak the token."""
        tg = tg_api.Telegram(self.TOKEN)
        boom = requests.ConnectionError(f"conn to /bot{self.TOKEN}/getMe reset")
        with mock.patch.object(tg.s, "post", side_effect=boom), \
             mock.patch.object(bp.time, "sleep", lambda s: None), \
             self.assertLogs("build_pack", level="WARNING") as caught:
            with self.assertRaises(RuntimeError):
                tg._call("getMe", retries=2)
        line = "\n".join(caught.output)
        self.assertIn("net retry", line)
        self.assertNotIn(self.TOKEN, line)
        self.assertIn("[REDACTED]", line)
