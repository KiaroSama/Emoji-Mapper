"""coins/verify_logos: the review sweep, and `--fix` as a live pack mutation.

`--fix` replaces a sticker inside a published pack and then rewrites the
canonical map to name the new id. There is no undo. So the whole module is about
proof: the review list must flag a different COIN rather than a restyled one,
and a replacement may only be recorded once its identity, its position and the
map it belongs to have all been verified -- with a persisted intent whenever the
run cannot finish the story itself.

The canonical map's other writers are in `test_coin_ticker_map`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from emojikit import build_pack as bp  # noqa: E402
from emojikit import telegram_api as tg_api  # noqa: E402
from emojikit import packstate as ps  # noqa: E402
from emojikit.build_pack import EXIT_FAILED, EXIT_OK, EXIT_USAGE  # noqa: E402
from tests._cli_fixtures import _load_standalone, _noise_png_bytes  # noqa: E402

OLD_CID, NEW_CID = "cid-old", "cid-new"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeDownload:
    """What ``Telegram.download_file`` expects back from ``session.get``."""

    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class ReplaceSession:
    """requests.Session stand-in serving ONE sticker set, with file downloads.

    ``lose_reply`` models the dangerous case: Telegram APPLIES the replacement
    and the response is lost on the way back. A blind retry then re-sends a
    non-idempotent call against an ``old_sticker`` that no longer exists.

    ``lose_confirm`` models the worse one: the call succeeds and the
    POSTCONDITION read never comes back, so the run cannot learn the new id --
    while the canonical map still names the old one, which no longer exists.
    """

    def __init__(self, before: list[str], after: list[str], *,
                 lose_reply: bool = False, lose_confirm: bool = False,
                 source: Path | None = None, images: dict | None = None):
        self.stickers = [{"custom_emoji_id": c, "file_id": f"fid-{c}"}
                         for c in before]
        self.after = [{"custom_emoji_id": c, "file_id": f"fid-{c}"} for c in after]
        self.lose_reply = lose_reply
        self.lose_confirm = lose_confirm
        # Every sticker serves the CURRENT bytes of the prepared source unless
        # `images` overrides it for a file_id.
        self.source = source
        self.images = dict(images or {})
        self.replaced = False
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(data or {}), dict(files or {})))
        if method == "getStickerSet":
            if self.lose_confirm and self.replaced:
                raise requests.ConnectionError("connection reset by peer")
            return FakeResponse({"ok": True, "result": {
                "name": data["name"], "stickers": self.stickers}})
        if method == "replaceStickerInSet":
            self.replaced = True
            self.stickers = list(self.after)          # Telegram applied it...
            if self.lose_reply:                       # ...and the reply vanished
                raise requests.ConnectionError("connection reset by peer")
            return FakeResponse({"ok": True, "result": True})
        if method == "getFile":
            return FakeResponse({"ok": True, "result": {
                "file_path": f"live/{data['file_id']}"}})
        raise AssertionError(f"unexpected Bot API method: {method}")

    def get(self, url, timeout=None):
        file_id = url.rsplit("/", 1)[-1]
        body = self.images.get(file_id)
        if body is None and self.source is not None:
            body = self.source.read_bytes()
        return FakeDownload(body or b"")

    def method(self, name: str) -> list[tuple[str, dict, dict]]:
        return [c for c in self.calls if c[0] == name]


class LogoReviewDistance(unittest.TestCase):
    """The review sweep must flag a different COIN, not a different palette.

    This project's emoji are drawn light-on-transparent so they read on
    Telegram's dark background; the reference art is dark-on-light. dHash is
    inversion-sensitive, so a plain comparison scored the SAME mark as maximally
    different -- a sweep of the top 1000 coins flagged 237, and every one of the
    twelve worst collapsed from ~50 to ~12 once inversion was accounted for
    (xrp 52 -> 12, bora 50 -> 10, xdai 47 -> 9). A review list that is mostly
    styling is a list nobody can act on.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "verify_logos.py",
                                   "coins_verify_logos_distance")

    def test_the_same_mark_inverted_is_not_reported_as_different(self):
        theirs = Image.open(io.BytesIO(_noise_png_bytes("some-coin")))
        ours = ImageOps.invert(theirs.convert("RGB"))
        plain = self.mod.hamming(self.mod.dh(ours), self.mod.dh(theirs))
        self.assertGreater(plain, 20,
                           "fixture is wrong: inversion must look far apart to "
                           "a plain dHash, or this proves nothing")
        self.assertLessEqual(self.mod.logo_distance(ours, theirs), 8)

    def test_a_genuinely_different_image_is_still_far(self):
        ours = Image.open(io.BytesIO(_noise_png_bytes("solana")))
        theirs = Image.open(io.BytesIO(_noise_png_bytes("solama-the-llama")))
        self.assertGreater(self.mod.logo_distance(ours, theirs), 20,
                           "tolerating restyling must not tolerate a different "
                           "coin -- that is the bug this tool exists to find")


class VerifyLogosFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "verify_logos.py",
                                   "coins_verify_logos_probe")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.emoji = self.tmp / "emoji"
        self.emoji.mkdir()
        Image.new("RGBA", (48, 48), (10, 200, 40, 255)).save(self.emoji / "btc.png")
        self.src = self.emoji / "btc.png"
        # Two tickers share one custom emoji: BOTH must be repointed, and only
        # at an identity we can prove.
        self.map_path = self.tmp / "ticker_to_id.json"
        self.map_path.write_text(json.dumps(
            {"btc": OLD_CID, "wbtc": OLD_CID, "eth": "cid-eth"}), encoding="utf-8")
        self.sets = [{"name": "cryptoemoji1_by_bot", "index": 1}]
        self.patches = [
            mock.patch.object(self.mod, "ROOT", self.tmp),
            mock.patch.object(self.mod, "fetch_markets", lambda top: [
                {"symbol": "btc", "name": "Bitcoin", "image": "http://x/btc.png"}]),
            # Noise, not a flat colour: identity has to be provable at all.
            mock.patch.object(self.mod, "fetch_image",
                              lambda url: _noise_png_bytes("official-btc")),
            mock.patch.object(bp.time, "sleep", lambda *_a: None),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mapping(self) -> dict:
        return json.loads(self.map_path.read_text(encoding="utf-8"))

    def intent(self) -> dict | None:
        path = self.mod._intent_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _session(self, before, after, **kw):
        session = ReplaceSession(before, after, source=self.src, **kw)
        tg = tg_api.Telegram("unit-test-token")
        tg.s = session
        return tg, session

    def _fix(self, before, after, state_path=None, **kw):
        tg, session = self._session(before, after, **kw)
        ok = self.mod.fix_one(tg, 42, self.sets, self.map_path, self.emoji,
                              "btc", state_path)
        return ok, session

    def test_timeout_after_apply_is_verified_not_resent(self):
        ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                                lose_reply=True)
        self.assertTrue(ok)
        # Exactly one attempt: the applied_check saw the change live, so the
        # non-idempotent call was never repeated.
        self.assertEqual(len(session.method("replaceStickerInSet")), 1)
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_the_uploaded_body_is_bytes_a_retry_can_resend(self):
        _, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                               lose_reply=True)
        _, _, files = session.method("replaceStickerInSet")[0]
        _name, body, _mime = files["file0"]
        # An open handle is exhausted after attempt 1: every retry would have
        # uploaded an empty sticker.
        self.assertIsInstance(body, bytes)
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_every_entry_sharing_the_old_cid_is_repointed(self):
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(ok)
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent(), "a settled replacement leaves no intent")

    def test_a_shifted_set_is_not_trusted_as_the_replacement(self):
        """Someone deleted an earlier sticker while we replaced ours.

        Position 1 now holds "c", an unrelated emoji. Reading live[pos] blindly
        repointed btc AND wbtc at it, silently and permanently.
        """
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], [NEW_CID, "c"])
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)

    def test_an_unchanged_set_is_not_trusted_either(self):
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", OLD_CID, "c"])
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)
        self.assertIsNone(self.intent(),
                          "nothing was applied, so nothing is pending")

    def test_a_replacement_whose_image_is_not_ours_is_refused(self):
        """The cid list is exactly what a clean replacement looks like.

        Structure is not identity: position 1 carries somebody else's art, and
        trusting the list shape repoints btc AND wbtc onto it forever.
        """
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                          images={f"fid-{NEW_CID}": _noise_png_bytes("stranger")})
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)

    def test_a_lost_confirmation_read_leaves_a_recoverable_intent(self):
        """Telegram applied the replacement; the postcondition read never came.

        Without a persisted intent the map keeps OLD_CID -- an id that is gone
        -- and no later run can find it, so the ticker is stranded for good.
        """
        before = self.mapping()
        ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                                lose_confirm=True)
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before, "nothing may be guessed here")
        intent = self.intent()
        self.assertIsNotNone(intent, "the pending replacement must be recorded")
        self.assertEqual(intent["operation"], "replace")
        self.assertEqual(intent["key"], "btc")
        self.assertEqual(intent["old_cid"], OLD_CID)
        self.assertEqual(intent["set_name"], session.calls[0][1]["name"])
        self.assertEqual(intent["set_index"], 1)
        self.assertEqual(intent["before"], ["a", OLD_CID, "c"])

    def test_the_next_run_recovers_the_replacement_from_the_intent(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        # Restart: the pack is in its post-replacement state and readable again.
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent(), "a recovered intent must be cleared")

    def test_recovery_refuses_when_the_live_image_is_not_ours(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        before = self.mapping()
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"],
                              images={f"fid-{NEW_CID}": _noise_png_bytes("stranger")})
        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), before)
        self.assertIsNotNone(self.intent(),
                             "an unresolved intent must survive for review")

    def test_recovery_is_idempotent_after_the_map_was_already_repointed(self):
        """Crashed after the map write, before the intent was cleared."""
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        self.mod.repoint(self.map_path, OLD_CID, NEW_CID)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent())

    def test_an_unreadable_set_leaves_the_intent_for_the_run_after(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        tg, session = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        session.lose_confirm = session.replaced = True   # every read fails
        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertIsNotNone(self.intent())

    def test_an_intent_cannot_be_reconciled_into_a_different_map(self):
        """7: the intent file has one fixed path; --map is chosen per run.

        A crash under ``--map A`` and a restart under ``--map B`` repointed the
        pending replacement inside B -- a file that never held the old id --
        while A kept naming a sticker that no longer exists.
        """
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        other = self.tmp / "other_map.json"
        other.write_text(json.dumps({"btc": OLD_CID}), encoding="utf-8")
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])

        self.assertFalse(self.mod.reconcile_intent(tg, other))
        self.assertEqual(json.loads(other.read_text("utf-8")), {"btc": OLD_CID},
                         "a map this replacement never touched was rewritten")
        self.assertIsNotNone(self.intent(),
                             "the intent still belongs to the original map")
        # ...and it is still recoverable into the map it actually names.
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_an_intent_is_bound_to_its_state_file_too(self):
        state_a, state_b = self.tmp / "state_a.json", self.tmp / "state_b.json"
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                  state_path=state_a, lose_confirm=True)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        before = self.mapping()

        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path, state_b))
        self.assertEqual(self.mapping(), before)
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path, state_a))
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_an_intent_with_no_recorded_target_is_refused(self):
        """An older intent cannot prove which map it belongs to: fail closed."""
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        path = self.mod._intent_path()
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("map_target"), legacy.pop("state_target")
        ps.write_json_atomic(path, legacy)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        before = self.mapping()

        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), before)
        self.assertIsNotNone(self.intent(), "an unresolved intent must survive")

    def test_a_map_writer_cannot_repoint_the_ticker_mid_replacement(self):
        """8: the id was chosen from the map with no lock held.

        alias_map / enhance_map / remap_ids --apply rewrite ticker_to_id.json
        under canonical_map_lock() alone, so one of them landing between the
        read and the repoint left --fix replacing the live sticker for an id
        the map no longer names: the sticker is destroyed and the repoint,
        which looks entries up by the OLD id, finds nothing to move.

        btc alone is on OLD_CID here so the race leaves nothing at all to
        update -- the honest outcomes are "the replacement is what the map now
        names" or "the run refused".
        """
        other = "cid-written-by-the-other-tool"
        self.map_path.write_text(json.dumps({"btc": OLD_CID}), encoding="utf-8")
        real = ps.canonical_map_lock

        @contextlib.contextmanager
        def racing():
            """The map-only writer gets in the instant this run takes the lock."""
            with real() as beat:
                mp = json.loads(self.map_path.read_text("utf-8"))
                mp["btc"] = other
                ps.write_json_atomic(self.map_path, mp)
                yield beat

        with mock.patch.object(self.mod, "canonical_map_lock", racing):
            ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])

        if session.method("replaceStickerInSet"):
            self.assertTrue(ok)
            self.assertEqual(self.mapping()["btc"], NEW_CID,
                             "a live sticker was replaced for an id the map "
                             "had already been repointed away from")
        else:
            self.assertFalse(ok)
            self.assertEqual(self.mapping(), {"btc": other})

    def test_a_busy_map_lock_stops_the_fix_before_it_touches_the_pack(self):
        """No live replacement may happen that cannot then be repointed.

        The lock has to be taken BEFORE the id is read, so a map editor already
        holding it stops this run while the packs are still untouched. Taking
        it only at the repoint meant the sticker was long gone by the time the
        run discovered it could not record what it had done.
        """
        before = self.mapping()
        tg, session = self._session(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])
        with ps.canonical_map_lock(), self.assertRaises(ps.LockBusy):
            self.mod.fix_one(tg, 42, self.sets, self.map_path, self.emoji, "btc")
        self.assertEqual(session.method("replaceStickerInSet"), [],
                         "a sticker was replaced while the map was unwritable")
        self.assertEqual(self.mapping(), before)
        self.assertIsNone(self.intent())

    def test_fix_locks_on_the_pack_family_not_on_this_script(self):
        # --fix REPLACES stickers in the same cryptoemoji* sets the coin
        # fetchers append to. A lock named after this file was a different name
        # from theirs, so the exclusion it claimed never actually held.
        self.assertEqual(self.mod.SET_BASE, "cryptoemoji")
        self.assertEqual(self.mod.PACK_LOCK,
                         ps.pack_family_lock_path(self.mod.SET_BASE))

    def test_verified_new_cid_rules(self):
        v = self.mod.verified_new_cid
        self.assertEqual(v(["a", OLD_CID], ["a", NEW_CID], 1), NEW_CID)
        self.assertIsNone(v(["a", OLD_CID], [NEW_CID], 1))          # length drift
        self.assertIsNone(v(["a", OLD_CID], ["z", NEW_CID], 1))     # neighbour moved
        self.assertIsNone(v(["a", OLD_CID], ["a", "a"], 1))         # id already present
        self.assertIsNone(v(["a", OLD_CID], ["a", OLD_CID], 1))     # nothing changed


class VerifyLogosMainContracts(unittest.TestCase):
    """--fix exit code and configuration errors."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "verify_logos.py",
                                   "coins_verify_logos_main")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.emoji = self.tmp / "emoji"
        self.emoji.mkdir()
        (self.tmp / "state.json").write_text(json.dumps(
            {"sets": [{"name": "cryptoemoji1_by_bot", "index": 1}]}),
            encoding="utf-8")
        (self.tmp / "map.json").write_text(json.dumps({"sol": OLD_CID}),
                                           encoding="utf-8")
        self.patches = [
            mock.patch.object(self.mod, "setup_logging", lambda *a, **k: None),
            mock.patch.object(self.mod, "load_env", lambda *a, **k: None),
            # ROOT anchors the in-flight intent file: keep it out of the repo.
            mock.patch.object(self.mod, "ROOT", self.tmp),
            mock.patch.object(self.mod, "PACK_LOCK", self.tmp / "pack.lock"),
            # The requested ticker is simply not in the market data: fix_one
            # returns False without touching Telegram.
            mock.patch.object(self.mod, "fetch_markets", lambda top: []),
            mock.patch.object(self.mod.time, "sleep", lambda *_a: None),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, env: dict, *extra) -> int:
        argv = ["verify_logos.py", "--emoji-dir", str(self.emoji),
                "--map", str(self.tmp / "map.json"),
                "--state", str(self.tmp / "state.json"), *extra]
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(io.StringIO()):
            return self.mod.main()

    def test_the_set_list_is_read_after_the_pack_lock_not_before(self):
        """A rebuild finishing during the wait invalidates the pre-lock snapshot.

        --state is read to build the set list, then the run waits for
        PACK_LOCK. A rebuild that completes in that window deletes the old packs
        and writes a new set list, and the map already points at the new family
        -- so acting on the snapshot searches packs that no longer exist and
        reports the ticker as missing instead of repairing it.
        """
        state = self.tmp / "state.json"
        state.write_text(json.dumps(
            {"sets": [{"name": "old_pack_1", "index": 1}]}), encoding="utf-8")
        (self.tmp / "map.json").write_text(json.dumps({"btc": OLD_CID}),
                                           encoding="utf-8")
        seen: list[list[str]] = []

        real_lock = self.mod.exclusive_lock

        @contextlib.contextmanager
        def rebuild_finishes_while_we_wait(path, **kw):
            # The rebuild lands its new state exactly while this run blocks.
            state.write_text(json.dumps(
                {"sets": [{"name": "rebuilt_pack_1", "index": 1}]}),
                encoding="utf-8")
            with real_lock(path, **kw) as beat:
                yield beat

        def record(tg, uid, sets, *a, **kw):
            seen.append([s["name"] for s in sets])
            return False

        with mock.patch.object(self.mod, "exclusive_lock",
                               rebuild_finishes_while_we_wait), \
                mock.patch.object(self.mod, "fix_one", record), \
                mock.patch.object(self.mod, "reconcile_intent",
                                  lambda *a, **k: True):
            self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                       "--fix", "--only", "btc")

        self.assertEqual(seen, [["rebuilt_pack_1"]],
                         "the fix ran against the set list captured BEFORE the "
                         "lock; those packs no longer exist")

    def test_a_requested_fix_that_failed_exits_nonzero(self):
        code = self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                          "--fix", "--only", "sol")
        # Nothing was repaired although a repair was explicitly requested.
        self.assertEqual(code, EXIT_FAILED)

    def test_missing_owner_id_is_a_usage_error_not_a_traceback(self):
        env = {"TELEGRAM_BOT_TOKEN": "t"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PACK_OWNER_USER_ID", None)
            self.assertEqual(
                self._main(env, "--fix", "--only", "sol"), EXIT_USAGE)

    def test_non_numeric_owner_id_is_a_usage_error(self):
        code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                           "PACK_OWNER_USER_ID": "not-a-number"},
                          "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_USAGE)

    def test_missing_token_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            self.assertEqual(
                self._main({"PACK_OWNER_USER_ID": "7"}, "--fix", "--only", "sol"),
                EXIT_USAGE)

    def test_unreadable_state_file_is_a_usage_error(self):
        (self.tmp / "state.json").write_text("{not json", encoding="utf-8")
        code = self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                          "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_USAGE)

    def test_an_unresolved_intent_blocks_every_replacement(self):
        """The next fix would overwrite the only record of the pending one."""
        attempted: list[tuple] = []
        with mock.patch.object(self.mod, "reconcile_intent", lambda *a: False), \
                mock.patch.object(self.mod, "fix_one",
                                  lambda *a: attempted.append(a) or True):
            code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                               "PACK_OWNER_USER_ID": "7"},
                              "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(attempted, [], "no pack may be mutated first")

    def test_a_clear_intent_lets_the_run_proceed(self):
        seen: list[tuple] = []
        with mock.patch.object(self.mod, "reconcile_intent", lambda *a: True), \
                mock.patch.object(self.mod, "fix_one",
                                  lambda *a: seen.append(a) or True):
            code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                               "PACK_OWNER_USER_ID": "7"},
                              "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
