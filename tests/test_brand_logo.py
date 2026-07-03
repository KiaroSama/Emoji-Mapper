"""Tests for the mandatory brand-logo-first behaviour in build_collection.

Covers:
  * BrandLogo format conversion (static PNG, looped video WEBM, animated needs
    a Lottie source and is skipped for raster);
  * publish_format placing the logo as the FIRST emoji of every set, only for
    the Emoji Mapper bot, with correct custom_emoji_id offset mapping.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import build_collection as bc  # noqa: E402
from emojikit import media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    for x in range(20, 100):
        for y in range(20, 100):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


class FakeTelegram:
    """Minimal in-memory stand-in for the Telegram client used by publish_format."""

    def __init__(self, username="YourEmojiBot"):
        self.username = username
        self.sets: dict[str, list[dict]] = {}
        self.messages: list[str] = []

    def get_me(self):
        return {"username": self.username}

    def create_emoji_set(self, user_id, name, title, path, fmt, emojis, keywords):
        self.sets[name] = [{"emojis": list(emojis),
                            "custom_emoji_id": f"{name}-0"}]

    def add_emoji(self, user_id, name, path, fmt, emojis, keywords):
        i = len(self.sets[name])
        self.sets[name].append({"emojis": list(emojis),
                                "custom_emoji_id": f"{name}-{i}"})

    def get_sticker_set(self, name):
        return {"stickers": self.sets.get(name, [])}

    def send_message(self, user_id, text):
        self.messages.append(text)


class BrandLogoConversion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.png = self.data / "logo_src.png"
        _make_png(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_static_logo_is_100x100_png(self):
        logo = bc.BrandLogo(str(self.png), self.data)
        out = logo.for_format("static")
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        with Image.open(out) as im:
            self.assertEqual(im.size, (media.SIZE, media.SIZE))

    def test_video_logo_is_valid_webm(self):
        logo = bc.BrandLogo(str(self.png), self.data)
        out = logo.for_format("video")
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        media.validate_video(out)  # raises if not a compliant VP9 100x100 webm

    def test_animated_without_lottie_is_skipped(self):
        logo = bc.BrandLogo(str(self.png), self.data)
        self.assertIsNone(logo.for_format("animated"))
        self.assertIn("animated", logo._warned)

    def test_missing_source_returns_none(self):
        logo = bc.BrandLogo(str(self.data / "nope.png"), self.data)
        self.assertFalse(logo.available())
        self.assertIsNone(logo.for_format("static"))


class PublishFormatLogoFirst(unittest.TestCase):
    def _setup_catalog(self, tmp: Path, n=2):
        data = tmp
        keys = []
        with Catalog(data / "catalog.db") as cat:
            for i in range(n):
                p = data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, 20 * (i + 1), 200, 255))
                key = f"s:item{i:030d}"
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["😀"], keywords=[f"item{i}"])
                keys.append(key)
        return keys

    def test_logo_is_first_and_cids_offset(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._setup_catalog(data)
            logo_png = data / "logo.png"
            _make_png(logo_png, color=(0, 200, 0, 255))
            tg = FakeTelegram("YourEmojiBot")
            logo = bc.BrandLogo(str(logo_png), data)
            state = {"base": "pk", "sets": [], "sent": []}
            with Catalog(data / "catalog.db") as cat:
                bc.publish_format(tg, cat, fmt="static", plan_keys=keys,
                                  base="pk", title="Pack", user_id=1,
                                  default_emoji="😀", per_set=200,
                                  data_dir=data, state=state, bot="YourEmojiBot",
                                  logo=logo)
                set_name = "pks1_by_YourEmojiBot"
                stickers = tg.sets[set_name]
                # First emoji must be the brand logo.
                self.assertEqual(stickers[0]["emojis"], [bc.BRAND_LOGO_EMOJI])
                self.assertEqual(len(stickers), 3)  # logo + 2 items
                # State records the logo flag.
                self.assertTrue(state["sets"][0]["logo"])
                # cids map with offset 1 (item0 -> position1, item1 -> position2).
                self.assertEqual(cat.get(keys[0]).custom_emoji_id, f"{set_name}-1")
                self.assertEqual(cat.get(keys[1]).custom_emoji_id, f"{set_name}-2")

    def test_no_logo_when_disabled(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._setup_catalog(data)
            tg = FakeTelegram("YourEmojiBot")
            state = {"base": "pk", "sets": [], "sent": []}
            with Catalog(data / "catalog.db") as cat:
                bc.publish_format(tg, cat, fmt="static", plan_keys=keys,
                                  base="pk", title="Pack", user_id=1,
                                  default_emoji="😀", per_set=200,
                                  data_dir=data, state=state, bot="YourEmojiBot",
                                  logo=None)
                set_name = "pks1_by_YourEmojiBot"
                stickers = tg.sets[set_name]
                self.assertEqual(len(stickers), 2)          # no logo prepended
                self.assertFalse(state["sets"][0].get("logo"))
                self.assertEqual(cat.get(keys[0]).custom_emoji_id, f"{set_name}-0")


if __name__ == "__main__":
    unittest.main()
