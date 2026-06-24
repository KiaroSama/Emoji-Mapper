"""Tests for the blank-image guards in make_emoji_pngs (no blank emoji)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import make_emoji_pngs as m  # noqa: E402


class TestBlankGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_blank_transparent(self):
        self.assertTrue(m._is_blank(Image.new("RGBA", (100, 100), (0, 0, 0, 0))))

    def test_is_blank_opaque(self):
        self.assertFalse(m._is_blank(Image.new("RGBA", (100, 100), (10, 120, 200, 255))))

    def test_convert_raster_blank_returns_false(self):
        blank = self.tmp / "blank.png"
        Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(blank)
        out = self.tmp / "out.png"
        self.assertFalse(m._convert_raster(blank, out))
        self.assertFalse(out.exists())  # must NOT write a blank emoji

    def test_convert_raster_solid_returns_true_and_100(self):
        solid = self.tmp / "solid.png"
        Image.new("RGBA", (40, 70), (200, 30, 30, 255)).save(solid)
        out = self.tmp / "out.png"
        self.assertTrue(m._convert_raster(solid, out))
        with Image.open(out) as im:
            self.assertEqual(im.size, (100, 100))


if __name__ == "__main__":
    unittest.main()
