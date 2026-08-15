"""Regression tests for the coin logo providers (fetch_paprika / fetch_cmc).

These lock down the defects that put wrong or blank emoji into the live packs:

* a fully transparent provider image being accepted and uploaded as a blank,
* an add retried blindly after a timeout Telegram had already applied,
* emoji ids read off the TAIL of a set, which mis-assigns a ticker as soon as
  anything else appears in that set,
* a run with failed adds still reporting success,
* an ambiguous add whose verification ALSO failed leaving no record, so the
  next run added the same image again,
* a ticker another provider mapped while this run waited for the lock being
  uploaded a second time, because the upload loop walked the list built before
  the wait,
* coin tools locking on three different files while mutating one pack family,
* coins/verify_logos.py dying on an import that no longer exists.

No network, no real sleeps: Telegram is faked and every path is deterministic.
"""

from __future__ import annotations

import contextlib
import importlib
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

import build_pack as bp  # noqa: E402
from coins import fetch_cmc, fetch_paprika as fp, rebuild_dedup as rd  # noqa: E402

SET = "cryptoemoji1_by_bot"


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _gradient(reverse: bool = False) -> Image.Image:
    """Opaque left-to-right (or right-to-left) grey ramp.

    Two ramps of opposite direction are perceptually as far apart as a dHash can
    report, which is what makes the content match in the tests unambiguous.
    """
    img = Image.new("RGBA", (100, 100))
    px = img.load()
    for x in range(100):
        v = (99 - x) * 2 if reverse else x * 2
        for y in range(100):
            px[x, y] = (v, v, v, 255)
    return img


class FakeTelegram:
    """Deterministic stand-in for build_pack.Telegram."""

    def __init__(self, existing: int = 2):
        self.sets: dict[str, list[dict]] = {SET: []}
        self.blobs: dict[str, bytes] = {}
        self.adds: list[tuple[str, str, int | None]] = []
        self.creates: list[str] = []
        self.fail_add: Exception | None = None
        self.after_add = None          # hook: simulates a concurrent writer
        self.unreadable: set[str] = set()   # sets Telegram will not talk about
        self._n = 0
        for _ in range(existing):
            self.append(SET, _png_bytes(_gradient(reverse=True)))

    # ----- fake wire ------------------------------------------------------ #
    def append(self, name: str, data: bytes) -> dict:
        self._n += 1
        st = {"custom_emoji_id": f"c{self._n}", "file_id": f"f{self._n}",
              "file_unique_id": f"u{self._n}"}
        self.blobs[st["file_id"]] = data
        self.sets.setdefault(name, []).append(st)
        return st

    # ----- Telegram surface used by publish_logos ------------------------- #
    def get_me(self) -> dict:
        return {"username": "bot"}

    def get_sticker_set(self, name: str) -> dict:
        if name in self.unreadable:
            raise RuntimeError("getStickerSet failed after 5 attempts")
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": [dict(s) for s in self.sets[name]]}

    def probe_set_state(self, name: str):
        if name in self.unreadable:
            return bp.SetState.UNKNOWN, None
        if name not in self.sets:
            return bp.SetState.MISSING, None
        return bp.SetState.EXISTS, {"stickers": [dict(s) for s in self.sets[name]]}

    def add_sticker(self, user_id, name, png, emoji, keywords, *,
                    expected_before=None):
        self.adds.append((name, Path(png).stem, expected_before))
        if self.fail_add:
            raise self.fail_add
        self.append(name, Path(png).read_bytes())
        if self.after_add:
            self.after_add(self, name)

    def create_set(self, user_id, name, title, png, emoji, keywords):
        self.creates.append(name)
        self.sets.setdefault(name, [])
        self.append(name, Path(png).read_bytes())

    def download_file(self, file_id: str, dest: Path) -> Path:
        Path(dest).write_bytes(self.blobs[file_id])
        return dest


class BlankProviderLogo(unittest.TestCase):
    """A provider placeholder must never become an emoji."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "out.png"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fully_transparent_image_is_rejected(self):
        data = _png_bytes(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
        self.assertFalse(fp.to_emoji_png(data, self.dest))
        self.assertFalse(self.dest.exists(), "a blank emoji must not be written")

    def test_almost_empty_image_is_rejected(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(4):                       # below the 8-visible-pixel rule
            img.putpixel((x, 0), (255, 0, 0, 255))
        self.assertFalse(fp.to_emoji_png(_png_bytes(img), self.dest))
        self.assertFalse(self.dest.exists())

    def test_real_logo_is_still_accepted(self):
        self.assertTrue(fp.to_emoji_png(_png_bytes(_gradient()), self.dest))
        with Image.open(self.dest) as im:
            self.assertEqual((im.size, im.mode), ((100, 100), "RGBA"))


class VerifyLogosModule(unittest.TestCase):
    """coins/verify_logos.py died at import time on build_pack.API_BASE."""

    def test_module_imports_cleanly(self):
        mod = importlib.import_module("coins.verify_logos")
        self.assertTrue(callable(mod.main))

    def test_it_uses_the_shared_pack_lock(self):
        mod = importlib.import_module("coins.verify_logos")
        self.assertEqual(mod.PACK_LOCK, fp.PACK_LOCK,
                         "--fix mutates the same pack family as the fetchers")


class VerifiedPublish(unittest.TestCase):
    """The one shared publisher: duplicate-proof adds, ids by identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.emoji = self.dir / "emoji"
        self.emoji.mkdir()
        _gradient().save(self.emoji / "aaa.png", "PNG")
        self.state = self.dir / "state.json"
        bp.write_json_atomic(self.state, {"sets": [
            {"index": 1, "name": SET, "title": "T 1"}]})
        self.ids = self.dir / "ticker_to_id.json"
        bp.write_json_atomic(self.ids, {"btc": "c-btc"})
        self.lock = self.dir / "pack_cryptoemoji.lock"
        self.patch = mock.patch.multiple(
            fp, EMOJI=self.emoji, STATE=self.state, TICKER_IDS=self.ids,
            PACK_LOCK=self.lock, KEYWORDS_CSV=self.dir / "none.csv", USER_ID=1)
        self.patch.start()
        self.sleep = mock.patch.object(fp.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_the_live_count_is_sent_as_expected_before(self):
        """Without it a timeout after Telegram applied the add duplicates it."""
        tg = FakeTelegram(existing=2)
        mapping = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(tg.adds, [(SET, "aaa", 2)])

    def test_the_id_is_ours_even_when_another_sticker_appears(self):
        """The defect: cids[-1:] returns whatever landed last, not our upload."""
        tg = FakeTelegram(existing=2)
        alien = _png_bytes(_gradient(reverse=True))
        tg.after_add = lambda t, name: t.append(name, alien)

        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))

        live = tg.sets[SET]
        ours, tail = live[-2]["custom_emoji_id"], live[-1]["custom_emoji_id"]
        self.assertEqual(mapping["aaa"], ours)
        self.assertNotEqual(mapping["aaa"], tail, "tail attribution is the bug")
        self.assertEqual(json.loads(self.ids.read_text("utf-8"))["aaa"], ours)

    def test_an_unidentifiable_sticker_is_not_mapped(self):
        """Two indistinguishable new stickers: refuse rather than guess."""
        tg = FakeTelegram(existing=1)
        tg.after_add = lambda t, name: t.append(
            name, (self.emoji / "aaa.png").read_bytes())  # same image twice

        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertNotIn("aaa", mapping)

    def test_a_failed_add_is_neither_mapped_nor_counted_as_added(self):
        tg = FakeTelegram(existing=2)
        tg.fail_add = RuntimeError("addStickerToSet failed: STICKER_PNG_NOPNG")
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertNotIn("aaa", mapping)

    def test_an_ambiguous_add_is_resolved_from_live_state_not_re_sent(self):
        tg = FakeTelegram(existing=2)
        real_add = tg.add_sticker

        def ambiguous(*a, **kw):
            real_add(*a, **kw)                       # Telegram DID apply it
            raise bp.AmbiguousUploadError("addStickerToSet: network failure")

        tg.add_sticker = ambiguous
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(len(tg.adds), 1, "an ambiguous add must never be re-sent")
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])

    def test_a_full_set_rolls_over_and_the_new_set_is_recorded_after_it_exists(self):
        tg = FakeTelegram(existing=fp.PER_SET)
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(tg.creates, ["cryptoemoji2_by_bot"])
        sets = json.loads(self.state.read_text("utf-8"))["sets"]
        self.assertEqual([s["name"] for s in sets], [SET, "cryptoemoji2_by_bot"])
        self.assertEqual(mapping["aaa"],
                         tg.sets["cryptoemoji2_by_bot"][0]["custom_emoji_id"])

    def test_a_second_publisher_is_refused_instead_of_appending_too(self):
        self.lock.write_text("pid=1 started=now\n", encoding="utf-8")
        tg = FakeTelegram(existing=2)
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertEqual(tg.adds, [], "the lock must stop the second run")

    def test_a_rebuild_holding_the_family_lock_blocks_a_top_up(self):
        """Only real if both tools name the SAME lock (see OnePackFamilyOneLock)."""
        tg = FakeTelegram(existing=2)
        with mock.patch.object(rd, "LOCK", self.lock), \
             bp.exclusive_lock(rd.LOCK):
            self.assertEqual(fp.publish_logos(tg, ["aaa"], {}), (0, 1))
        self.assertEqual(tg.adds, [])


class TheCanonicalMapIsRereadUnderTheLock(VerifiedPublish):
    """6: the providers load ticker_to_id.json BEFORE waiting for the lock.

    Two coin tools therefore serialised their Telegram mutations and still lost
    each other's map update: whichever went second wrote the whole file back
    from a snapshot taken before the first one had finished. The publisher has
    to re-read the map once it holds the locks -- and hold the map lock for the
    batch, because its per-sticker writes are one read-modify-write.
    """

    CONCURRENT = {"zzz": "written-by-the-other-tool"}

    def _racing_lock(self, concurrent: dict | None = None):
        """The other tool's map update lands as this run takes the lock."""
        real = fp.canonical_map_lock
        landed = self.CONCURRENT if concurrent is None else concurrent

        @contextlib.contextmanager
        def racing():
            with real() as beat:
                current = json.loads(self.ids.read_text("utf-8"))
                current.update(landed)
                bp.write_json_atomic(self.ids, current)
                yield beat

        return mock.patch.object(fp, "canonical_map_lock", racing)

    def test_neither_writers_ids_are_lost(self):
        tg = FakeTelegram(existing=2)
        stale = json.loads(self.ids.read_text("utf-8"))   # loaded before the lock
        with self._racing_lock():
            self.assertEqual(fp.publish_logos(tg, ["aaa"], stale), (1, 0))
        saved = json.loads(self.ids.read_text("utf-8"))
        self.assertEqual(saved["btc"], "c-btc", "the pre-existing id is gone")
        self.assertEqual(saved["zzz"], self.CONCURRENT["zzz"],
                         "the other tool's id was overwritten from a stale read")
        self.assertEqual(saved["aaa"], tg.sets[SET][-1]["custom_emoji_id"])
        self.assertEqual(stale["zzz"], self.CONCURRENT["zzz"],
                         "the caller refills the inventory from this dict, so "
                         "it must be refreshed in place too")

    def test_a_ticker_mapped_while_we_waited_is_not_uploaded_again(self):
        """Re-reading the map only helps if the ticker list is re-filtered too.

        Two providers both saw 'aaa' unmapped and both downloaded it. The one
        that won the lock uploaded it and mapped it; this one must not add the
        same coin a second time -- that is a duplicate emoji in the live pack,
        and 'aaa' would end up naming the copy instead of the sticker that is
        already published.
        """
        tg = FakeTelegram(existing=2)
        stale = json.loads(self.ids.read_text("utf-8"))   # loaded before the lock
        with self._racing_lock({"aaa": "c-from-the-other-provider"}):
            self.assertEqual(fp.publish_logos(tg, ["aaa"], stale), (0, 0))
        self.assertEqual(tg.adds, [], "the coin is already live in the pack")
        self.assertEqual(len(tg.sets[SET]), 2, "no second copy may be added")
        saved = json.loads(self.ids.read_text("utf-8"))
        self.assertEqual(saved["aaa"], "c-from-the-other-provider",
                         "the published sticker's id was overwritten")
        self.assertEqual(stale["aaa"], "c-from-the-other-provider",
                         "the caller refills the inventory from this dict")

    def test_the_rest_of_the_batch_is_still_published(self):
        """Skipping the coin that was taken must not abandon the others."""
        tg = FakeTelegram(existing=2)
        (self.emoji / "bbb.png").write_bytes((self.emoji / "aaa.png").read_bytes())
        stale = json.loads(self.ids.read_text("utf-8"))
        with self._racing_lock({"aaa": "c-from-the-other-provider"}):
            self.assertEqual(fp.publish_logos(tg, ["aaa", "bbb"], stale), (1, 0))
        self.assertEqual([a[1] for a in tg.adds], ["bbb"])
        self.assertEqual(stale["bbb"], tg.sets[SET][-1]["custom_emoji_id"])

    def test_a_map_writer_blocks_the_publisher(self):
        """Proof the publisher takes the map lock at all, in the right order."""
        tg = FakeTelegram(existing=2)
        with bp.canonical_map_lock():
            self.assertEqual(fp.publish_logos(tg, ["aaa"], {}), (0, 1))
        self.assertEqual(tg.adds, [], "no sticker may be added without it")
        self.assertFalse(self.lock.exists(),
                         "the pack lock must be released, not stranded")


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
        bp.write_json_atomic(self.state, {"sets": [
            {"index": 1, "name": SET, "title": "T 1"}]})
        self.ids = self.dir / "ticker_to_id.json"
        bp.write_json_atomic(self.ids, {})
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
            raise bp.AmbiguousUploadError("addStickerToSet: network failure")

        tg.add_sticker = ambiguous

    def test_the_unverified_upload_is_recorded_and_not_repeated(self):
        tg = FakeTelegram(existing=2)
        self._blind_after_apply(tg)
        mapping: dict[str, str] = {}

        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertNotIn("aaa", mapping, "the id was never verified")
        intent = json.loads(self.state.read_text("utf-8")).get("in_flight")

        tg.unreadable.clear()                  # the next run, Telegram is back
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (1, 0))
        self.assertEqual(len(tg.adds), 1,
                         "the image was already live; adding it again is the "
                         "duplicate this ledger exists to prevent")
        self.assertEqual(len(tg.sets[SET]), 3)
        self.assertEqual(mapping["aaa"], tg.sets[SET][-1]["custom_emoji_id"])
        self.assertEqual(json.loads(self.ids.read_text("utf-8"))["aaa"],
                         mapping["aaa"])
        self.assertIsNone(json.loads(self.state.read_text("utf-8"))["in_flight"])
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
        bp.write_json_atomic(self.ids, {**current, "aaa": "c-the-other-tools"})
        self.assertEqual(fp.publish_logos(FakeTelegram(existing=2), ["aaa"], {}),
                         (0, 0))
        self.assertEqual((self.emoji / "aaa.png").read_bytes(), published,
                         "a skipped upload replaced the identity oracle for a "
                         "sticker it did not publish")

        # (b) PUBLISHED: the same art, now actually uploaded and mapped, MUST
        #     become the oracle -- otherwise the map names a sticker the other
        #     tools cannot recognize, which is the same breakage from the other
        #     direction.
        bp.write_json_atomic(self.ids, current)
        tg = FakeTelegram(existing=2)
        self.assertEqual(fp.publish_logos(tg, ["aaa"], {}), (1, 0))
        self.assertNotEqual((self.emoji / "aaa.png").read_bytes(), published,
                            "the published art never became the oracle")
        self.assertFalse((fp.incoming_dir() / "aaa.png").exists(),
                         "staging must not keep a copy after promotion")

    def test_an_upload_that_never_landed_is_retried_once(self):
        """The mirror case: a recorded intent must not block a real retry."""
        tg = FakeTelegram(existing=2)
        real_add = tg.add_sticker

        def blind(*a, **kw):
            tg.unreadable.add(SET)             # dark BEFORE Telegram applied it
            raise bp.AmbiguousUploadError("addStickerToSet: network failure")

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
            json.loads(self.state.read_text("utf-8"))["in_flight"]["key"], "aaa")


class OnePackFamilyOneLock(unittest.TestCase):
    """One live pack family must mean one lock name, whatever the tool."""

    def test_every_coin_tool_locks_on_the_pack_base(self):
        family = bp.pack_family_lock_path(fp.SET_BASE)
        self.assertEqual(fp.PACK_LOCK, family)
        self.assertEqual(rd.LOCK, family,
                         "the rebuild appends to the very same sets")
        self.assertEqual(rd.BASE, fp.SET_BASE)


class CommandExitCodes(unittest.TestCase):
    """A run whose adds all failed must not look like a clean one."""

    INVENTORY = ("## AAA - Alpha Coin\n"
                 "  ticker: aaa\n"
                 "  premium-id:\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.emoji = self.dir / "emoji"
        self.emoji.mkdir()
        self.state = self.dir / "state.json"
        bp.write_json_atomic(self.state, {"sets": [
            {"index": 1, "name": SET, "title": "T 1"}]})
        self.ids = self.dir / "ticker_to_id.json"
        bp.write_json_atomic(self.ids, {"btc": "c-btc"})
        self.cache = self.dir / "cache.json"
        bp.write_json_atomic(self.cache, {
            "aaa": {"id": "alpha-coin", "conf": "symbol", "name": "Alpha Coin"}})
        self.inv = self.dir / "inv.md"
        self.inv.write_text(self.INVENTORY, encoding="utf-8")

        self.patch = mock.patch.multiple(
            fp, EMOJI=self.emoji, STATE=self.state, TICKER_IDS=self.ids,
            CACHE=self.cache, INV=self.inv, OUT_INV=self.dir / "out.md",
            PACK_LOCK=self.dir / "pack_cryptoemoji.lock",
            KEYWORDS_CSV=self.dir / "none.csv", USER_ID=1)
        self.patch.start()
        self.sleep = mock.patch.object(fp.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def _run(self, tg) -> int:
        argv = ["fetch_paprika.py"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "x"}, clear=False), \
             mock.patch.object(fp, "resolve_phase", return_value=False), \
             mock.patch.object(fp, "http_bytes",
                               return_value=_png_bytes(_gradient())), \
             mock.patch.object(fp, "Telegram", return_value=tg):
            return fp.main()

    def test_a_failed_add_exits_nonzero(self):
        tg = FakeTelegram(existing=2)
        tg.fail_add = RuntimeError("addStickerToSet failed: STICKER_PNG_NOPNG")
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)

    def test_a_blank_provider_logo_exits_nonzero(self):
        tg = FakeTelegram(existing=2)
        with mock.patch.object(fp, "http_bytes", return_value=_png_bytes(
                Image.new("RGBA", (64, 64), (0, 0, 0, 0)))):
            with mock.patch.object(sys, "argv", ["fetch_paprika.py"]), \
                 mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "x"},
                                 clear=False), \
                 mock.patch.object(fp, "resolve_phase", return_value=False), \
                 mock.patch.object(fp, "Telegram", return_value=tg):
                self.assertEqual(fp.main(), bp.EXIT_FAILED)
        self.assertEqual(tg.adds, [], "a blank logo must never reach Telegram")

    def test_a_clean_run_exits_zero_and_records_the_id(self):
        tg = FakeTelegram(existing=2)
        self.assertEqual(self._run(tg), bp.EXIT_OK)
        self.assertEqual(json.loads(self.ids.read_text("utf-8"))["aaa"],
                         tg.sets[SET][-1]["custom_emoji_id"])

    def test_both_fetchers_publish_through_the_same_helper(self):
        """Two copies of the add loop is how they drifted apart in the first place."""
        self.assertIs(fetch_cmc.publish_logos, fp.publish_logos)


if __name__ == "__main__":
    unittest.main()
