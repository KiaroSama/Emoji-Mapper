"""Tests for the curate panel's brand-logo preview (panel.build_view).

The brand logo is never part of the catalog (it's injected only at publish
time by build_collection.py), but the panel should still show a preview card
for it -- without letting it affect the real included/excluded counts or be
sent to /api/save. These tests lock down that separation.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import panel as p  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (40, 40), (10, 20, 30, 255)).save(path, "PNG")


class BrandLogoPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(3):
                img = self.data / "media" / "static" / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"s:item{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"item{i}"])
            self.cat_path = self.data / "catalog.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _view(self, bot_username: str, logo_file: Path | None):
        target = str(logo_file) if logo_file else p.BRAND_LOGO_DEFAULT
        with mock.patch.object(p, "BRAND_LOGO_DEFAULT", target):
            with Catalog(self.cat_path) as cat:
                return p.build_view(cat, bot_username)

    def test_logo_shown_first_for_emoji_mapper_bot(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, by_key = self._view("YourEmojiBot", logo)
        self.assertTrue(view[0]["isLogo"])
        self.assertEqual(view[0]["key"], p.LOGO_KEY)
        self.assertEqual(by_key[p.LOGO_KEY], logo)
        # The 3 real catalog items still follow, none marked as logo.
        self.assertEqual(len(view), 4)
        self.assertTrue(all(not v.get("isLogo") for v in view[1:]))

    def test_logo_hidden_for_coin_bot(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, _ = self._view("YourCoinEmojiBot", logo)
        self.assertEqual(len(view), 3)  # no logo card injected
        self.assertTrue(all(not v.get("isLogo") for v in view))

    def test_logo_hidden_when_file_missing(self):
        missing = self.data / "does_not_exist.png"
        view, _ = self._view("YourEmojiBot", missing)
        self.assertEqual(len(view), 3)

    def test_logo_hidden_when_bot_unknown(self):
        logo = self.data / "logo.png"; _make_png(logo)
        view, _ = self._view("", logo)
        self.assertEqual(len(view), 3)


if __name__ == "__main__":
    unittest.main()
