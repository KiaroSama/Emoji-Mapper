"""Regression tests for the coin logo providers (fetch_paprika / fetch_cmc).

These lock down the defects that put wrong or blank emoji into the live packs:

* a fully transparent provider image being accepted and uploaded as a blank,
* an add retried blindly after a timeout Telegram had already applied,
* emoji ids read off the TAIL of a set, which mis-assigns a ticker as soon as
  anything else appears in that set,
* a run with failed adds still reporting success,
* coins/verify_logos.py dying on an import that no longer exists.

No network, no real sleeps: Telegram is faked and every path is deterministic.
"""

from __future__ import annotations

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
from coins import fetch_cmc, fetch_paprika as fp  # noqa: E402

SET = "gvcryptoemoji1_by_bot"


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
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": [dict(s) for s in self.sets[name]]}

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
        self.lock = self.dir / "coin_pack.lock"
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
        self.assertEqual(mapping, {})

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
        self.assertEqual(tg.creates, ["gvcryptoemoji2_by_bot"])
        sets = json.loads(self.state.read_text("utf-8"))["sets"]
        self.assertEqual([s["name"] for s in sets], [SET, "gvcryptoemoji2_by_bot"])
        self.assertEqual(mapping["aaa"],
                         tg.sets["gvcryptoemoji2_by_bot"][0]["custom_emoji_id"])

    def test_a_second_publisher_is_refused_instead_of_appending_too(self):
        self.lock.write_text("pid=1 started=now\n", encoding="utf-8")
        tg = FakeTelegram(existing=2)
        mapping: dict[str, str] = {}
        self.assertEqual(fp.publish_logos(tg, ["aaa"], mapping), (0, 1))
        self.assertEqual(tg.adds, [], "the lock must stop the second run")


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
            PACK_LOCK=self.dir / "coin_pack.lock",
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
