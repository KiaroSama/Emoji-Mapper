"""fetch_paprika's unverified-upload recovery path.

Split from test_coin_providers.py: an add that MAY be live must be
reconciled against the live set, never silently re-sent.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import telegram_api as tg_api  # noqa: E402
from emojikit import packstate as ps  # noqa: E402
from coins import fetch_paprika as fp  # noqa: E402

SET = "cryptoemoji1_by_bot"

from tests._coin_fixtures import (FakeTelegram, _gradient,  # noqa: E402
                                  _png_bytes)


class UnverifiedUploadIsRecovered(unittest.TestCase):
    """An add that may be live must be reconciled, never silently re-sent.

    The ambiguous failure was already tolerated -- but only while the identity
    check that decides it succeeds. When live state went dark for that check
    too, the run just logged "add failed" and forgot: nothing on disk said an
    emoji might already be live, so the next run added the same image again.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.emoji = self.dir / "emoji"
        self.emoji.mkdir()
        _gradient().save(self.emoji / "aaa.png", "PNG")
        self.state = self.dir / "state.json"
        ps.write_json_atomic(self.state, {"sets": [
            {"index": 1, "name": SET, "title": "T 1"}]})
        self.ids = self.dir / "ticker_to_id.json"
        ps.write_json_atomic(self.ids, {})
        self.patch = mock.patch.multiple(
            fp, EMOJI=self.emoji, STATE=self.state, TICKER_IDS=self.ids,
            PACK_LOCK=self.dir / "pack.lock", KEYWORDS_CSV=self.dir / "none.csv",
            USER_ID=1)
        self.patch.start()
        self.sleep = mock.patch.object(fp.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def _blind_after_apply(self, tg):
        """Telegram applies the add, then stops answering about the set.

        Once only: the next run finds a healthy Telegram, which is exactly when
        an unrecorded upload gets sent a second time.
        """
        real_add = tg.add_sticker

        def ambiguous(*a, **kw):
            tg.add_sticker = real_add          # the outage is over after this
            real_add(*a, **kw)                 # the add DID land
            tg.unreadable.add(SET)             # ...and then the link went down
            raise tg_api.AmbiguousUploadError("addStickerToSet: network failure")

        tg.add_sticker = ambiguous

    def test_the_unverified_upload_is_recorded_and_not_repeated(self):
        tg = FakeTelegram(existing=2)
        self._blind_after_apply(tg)
        mapping: dict[str, str] = {}

        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertNotIn("aaa", mapping, "the id was never verified")
        intent = json.loads(self.state.read_text("utf-8")).get(fp.INTENT_KEY)

        tg.unreadable.clear()                  # the next run, Telegram is back
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(len(tg.adds), 1,
                         "the image was already live; adding it again is the "
                         "duplicate this ledger exists to prevent")
        self.assertEqual(len(tg.sets[SET]), 3)
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])
        self.assertEqual(json.loads(self.ids.read_text("utf-8"))["aaa"],
                         mapping["aaa"])
        self.assertIsNone(json.loads(self.state.read_text("utf-8"))[fp.INTENT_KEY])
        # ...and the record that made the recovery possible.
        self.assertEqual((intent or {}).get("key"), "aaa")
        self.assertEqual((intent or {}).get("operation"), "add")
        self.assertEqual((intent or {}).get("set_name"), SET)
        self.assertEqual((intent or {}).get("expected_before"), 2)

    def test_a_re_downloaded_source_does_not_unmake_the_recovery(self):
        """The recovery oracle must be the image we SENT, not the file's art now.

        EMOJI/<ticker>.png is shared and written by main() with no lock held, and
        main() re-downloads a ticker whenever the map has no entry -- which is
        exactly the state an unresolved upload leaves behind. Re-hashing the file
        at recovery time therefore compares the live sticker against the NEW art,
        concludes "it did not land", and uploads a second copy. No concurrency is
        needed: two sequential runs of the same tool do it.
        """
        tg = FakeTelegram(existing=2)
        self._blind_after_apply(tg)
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        live_before = len(tg.sets[SET])

        # Between the runs, the ticker is resolved again and the shared path is
        # overwritten with a DIFFERENT image for the same coin.
        _gradient(reverse=True).save(self.emoji / "aaa.png", "PNG")

        tg.unreadable.clear()
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(len(tg.sets[SET]), live_before,
                         "the already-live upload was sent a second time")
        self.assertEqual(len(tg.adds), 1)

    def test_a_skipped_ticker_does_not_replace_the_published_logo(self):
        """The oracle must keep describing the sticker the map names.

        coins/logos/emoji/<ticker>.png is how rebuild_dedup and remap_ids decide
        which live sticker belongs to which ticker. A losing provider that
        downloaded its own art for the same coin, then skipped the upload,
        used to leave that art behind while the map named the WINNER's sticker.
        Nothing is duplicated -- but resolve_by_image then reports
        MapIdentityUnproven and map_and_fill hard-stops until a human repairs it.
        """
        published = (self.emoji / "aaa.png").read_bytes()
        fresh = _png_bytes(_gradient(reverse=True))

        # (a) SKIPPED: another tool published this coin while we waited, so the
        #     map already names ITS sticker. Our download must stay in staging.
        fp.to_emoji_png(fresh, fp.incoming_dir() / "aaa.png")
        current = json.loads(self.ids.read_text("utf-8"))
        ps.write_json_atomic(self.ids, {**current, "aaa": "c-the-other-tools"})
        self.assertEqual(fp.publish_logos(FakeTelegram(existing=2), ["aaa"], {}),
                         (0, 0))
        self.assertEqual((self.emoji / "aaa.png").read_bytes(), published,
                         "a skipped upload replaced the identity oracle for a "
                         "sticker it did not publish")

        self.assertFalse((fp.incoming_dir() / "aaa.png").exists(),
                         "an unpublished download must not survive the run that "
                         "declined to publish it")

        # (b) PUBLISHED: the same art, now actually uploaded and mapped, MUST
        #     become the oracle -- otherwise the map names a sticker the other
        #     tools cannot recognize, which is the same breakage from the other
        #     direction.
        fp.to_emoji_png(fresh, fp.incoming_dir() / "aaa.png")
        ps.write_json_atomic(self.ids, current)
        tg = FakeTelegram(existing=2)
        self.assertEqual(fp.publish_logos(tg, ["aaa"], {}), (1, 0))
        self.assertNotEqual((self.emoji / "aaa.png").read_bytes(), published,
                            "the published art never became the oracle")
        self.assertFalse((fp.incoming_dir() / "aaa.png").exists(),
                         "staging must not keep a copy after promotion")

    def test_another_process_cannot_swap_the_image_mid_upload(self):
        """Staging is private per run, so there is no shared name to overwrite.

        The download happens before any lock, so with one staging path per
        TICKER a second fetcher could replace the file between this run's hash,
        its upload, its applied-check and its promotion -- four steps that must
        all describe the same image, or an ambiguous request is decided against
        the wrong picture and the emoji is duplicated.

        The other process is simulated at the worst moment: inside the upload,
        after the hash was taken, writing the file it WOULD have shared.
        """
        tg = FakeTelegram(existing=2)
        mine = _png_bytes(_gradient())
        fp.to_emoji_png(mine, fp.incoming_dir() / "aaa.png")
        theirs = _png_bytes(_gradient(reverse=True))
        real_add = tg.add_sticker

        def add_then_someone_else_downloads(*a, **kw):
            out = real_add(*a, **kw)
            # A concurrent fetcher, mid-run, resolving the SAME ticker.
            fp.to_emoji_png(theirs, fp.EMOJI.parent / ".incoming" / "aaa.png")
            fp.to_emoji_png(theirs, fp.EMOJI / "aaa.png")
            return out

        tg.add_sticker = add_then_someone_else_downloads
        self.assertEqual(fp.publish_logos(tg, ["aaa"], {}), (1, 0))
        self.assertEqual(len(tg.adds), 1, "the upload was sent more than once")
        # The oracle describes what WE uploaded, not what landed in the shared
        # location afterwards.
        published = (self.emoji / "aaa.png").read_bytes()
        self.assertEqual(
            fp._dhash(Image.open(io.BytesIO(published)).convert("RGBA")),
            fp._dhash(Image.open(io.BytesIO(mine)).convert("RGBA")),
            "the promoted oracle is the other process's image")

    def test_an_unresolved_upload_keeps_its_source_for_the_next_run(self):
        """End to end: run A leaves an unresolved upload, run B recovers it.

        Per-run staging removed a shared mutable path, but discarding the whole
        directory on the way out threw away the one file the NEXT run needs.
        The chain that produced: run A uploads image A and cannot confirm it;
        its staging is deleted; run B sees the ticker still unmapped, downloads
        a DIFFERENT image B for it, recovery correctly identifies live A from
        the recorded hash -- and then promotes B as the local oracle. The map
        would name the sticker A produced while coins/logos/emoji/<ticker>.png
        held B, and every identity-based tool downstream reads that file.
        """
        art_a = _png_bytes(_gradient())
        art_b = _png_bytes(_gradient(reverse=True))
        published_before = (self.emoji / "aaa.png").read_bytes()

        # --- run A: uploads A, then live state goes dark before confirmation --
        tg = FakeTelegram(existing=2)
        fp.to_emoji_png(art_a, fp.incoming_dir() / "aaa.png")
        self._blind_after_apply(tg)
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        intent = json.loads(self.state.read_text("utf-8"))[fp.INTENT_KEY]
        self.assertIsNotNone(intent, "the unresolved upload was not recorded")
        live_after_a = len(tg.sets[SET])

        # The recovery source must still be there. Everything else may go.
        staged = Path(intent["source_path"])
        self.assertTrue(staged.is_file(),
                        "the only image that can prove what run A uploaded was "
                        "deleted on the way out")
        self.assertEqual((self.emoji / "aaa.png").read_bytes(), published_before,
                         "an unconfirmed upload must not touch the oracle")

        # --- run B: a fresh process, a different image for the same ticker ----
        # Capture BEFORE overwriting: registering the cleanup afterwards restores
        # "run-b" onto itself, so every later test in the process keeps this
        # ticker's staging path and per-run isolation is silently switched off.
        self.addCleanup(setattr, fp, "_RUN_TOKEN", fp._RUN_TOKEN)
        fp._RUN_TOKEN = "run-b"                      # a second process's staging
        fp.to_emoji_png(art_b, fp.incoming_dir() / "aaa.png")
        tg.unreadable.clear()
        # (1, 0): the ticker is accounted for by the recovery, not by a new send.
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))

        self.assertEqual(len(tg.sets[SET]), live_after_a,
                         "the already-live upload was sent a second time")
        self.assertEqual(len(tg.adds), 1)
        # The map names run A's sticker...
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])
        # ...and the oracle must be run A's image, never run B's.
        oracle = (self.emoji / "aaa.png").read_bytes()
        self.assertEqual(fp._dhash(Image.open(io.BytesIO(oracle)).convert("RGBA")),
                         fp._dhash(Image.open(io.BytesIO(art_a)).convert("RGBA")),
                         "the local logo is the other run's image, so the map "
                         "and the oracle now describe different pictures")
        # Only now may the intent be gone.
        self.assertIsNone(json.loads(self.state.read_text("utf-8"))[fp.INTENT_KEY])

    def test_recovery_never_promotes_an_image_it_cannot_prove(self):
        """The retained source can still be lost -- a wiped temp, another machine.

        Recovery then falls back to whatever this run staged for the ticker,
        which is a DIFFERENT picture. The map is being pointed at the sticker
        the original image produced, so promoting the fallback would leave
        ticker_to_id and coins/logos/emoji/<ticker>.png describing different
        images. Fail closed instead, and say what to do.
        """
        art_a = _png_bytes(_gradient())
        art_b = _png_bytes(_gradient(reverse=True))
        published_before = (self.emoji / "aaa.png").read_bytes()

        tg = FakeTelegram(existing=2)
        fp.to_emoji_png(art_a, fp.incoming_dir() / "aaa.png")
        self._blind_after_apply(tg)
        mapping: dict[str, str] = {}
        fp.publish_logos(tg, ["aaa"], mapping)
        intent = json.loads(self.state.read_text("utf-8"))[fp.INTENT_KEY]

        # The retained source is gone anyway, and run B staged its own art.
        Path(intent["source_path"]).unlink()
        self.addCleanup(setattr, fp, "_RUN_TOKEN", fp._RUN_TOKEN)  # before, not after
        fp._RUN_TOKEN = "run-b"
        fp.to_emoji_png(art_b, fp.incoming_dir() / "aaa.png")
        tg.unreadable.clear()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fp.publish_logos(tg, ["aaa"], mapping)

        # The live sticker is still identified from the recorded hash...
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])
        # ...but the oracle is untouched rather than replaced by run B's image.
        self.assertEqual((self.emoji / "aaa.png").read_bytes(), published_before,
                         "an unprovable image was promoted as the oracle")
        self.assertIn("was NOT updated", out.getvalue(),
                      "the operator is not told the local logo is now stale")

    def test_an_upload_that_never_landed_is_retried_once(self):
        """The mirror case: a recorded intent must not block a real retry."""
        tg = FakeTelegram(existing=2)
        real_add = tg.add_sticker

        def blind(*a, **kw):
            tg.unreadable.add(SET)             # dark BEFORE Telegram applied it
            raise tg_api.AmbiguousUploadError("addStickerToSet: network failure")

        tg.add_sticker = blind
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))

        tg.add_sticker = real_add
        tg.unreadable.clear()
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(len(tg.sets[SET]), 3)
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])

    def test_nothing_else_is_mutated_while_an_outcome_is_unresolved(self):
        """An id that cannot be decided is unresolved, not merely 'failed'."""
        tg = FakeTelegram(existing=2)
        ours = (self.emoji / "aaa.png").read_bytes()
        # A concurrent writer lands the SAME image, so which sticker is ours
        # cannot be told apart.
        tg.after_add = lambda t, name: t.append(name, ours)
        for tk in ("bbb", "ccc"):
            (self.emoji / f"{tk}.png").write_bytes(ours)

        self.assertEqual(fp.publish_logos(tg, ["aaa", "bbb", "ccc"], {}),
                         (0, 3))
        self.assertEqual([a[1] for a in tg.adds], ["aaa"],
                         "a later add would overwrite the unresolved intent")
        self.assertEqual(
            json.loads(self.state.read_text("utf-8"))[fp.INTENT_KEY]["key"], "aaa")
