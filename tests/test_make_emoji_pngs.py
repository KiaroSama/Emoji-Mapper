"""Tests for make_emoji_pngs: blank guards, output freshness, source priority."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import make_emoji_pngs as m  # noqa: E402
from emojikit.build_pack import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL  # noqa: E402

BLUE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
            '<rect width="64" height="64" fill="#0000ff"/></svg>')
RED = (240, 20, 20, 255)
BLUE = (20, 20, 240, 255)
OLD = 1_700_000_000  # fixed mtimes keep the freshness tests off the wall clock


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


class TestGeneralRun(unittest.TestCase):
    """General mode: freshness, source priority, exit codes, quarantine."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "in"
        self.out = self.tmp / "out"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raster(self, name: str, color=RED, size=(40, 70)) -> Path:
        p = self.src / name
        Image.new("RGBA", size, color).save(p)
        return p

    def _run(self, limit: int = 0) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return m._run_general(self.src, self.out, limit)

    def _center(self, name: str = "a.png"):
        with Image.open(self.out / name) as im:
            return im.convert("RGBA").getpixel((50, 50))

    @staticmethod
    def _age(path: Path, when: int) -> None:
        os.utime(path, (when, when))

    # --- reuse vs rebuild ------------------------------------------------

    def test_valid_output_is_reused(self):
        src = self._raster("a.png")
        self.assertEqual(self._run(), EXIT_OK)
        out = self.out / "a.png"
        self._age(out, OLD)
        self._age(src, OLD - 60)
        self.assertEqual(self._run(), EXIT_OK)
        self.assertEqual(int(out.stat().st_mtime), OLD)  # untouched

    def test_newer_source_is_reconverted(self):
        src = self._raster("a.png", RED)
        self._run()
        out = self.out / "a.png"
        Image.new("RGBA", (40, 70), BLUE).save(src)  # source edited after conversion
        self._age(out, OLD)
        self._age(src, OLD + 60)
        self.assertEqual(self._run(), EXIT_OK)
        px = self._center()
        self.assertGreater(px[2], px[0])  # rebuilt from the new (blue) source

    def test_corrupt_output_is_rebuilt(self):
        self._raster("a.png")
        self.out.mkdir(parents=True)
        (self.out / "a.png").write_bytes(b"not a png at all")
        self.assertEqual(self._run(), EXIT_OK)
        with Image.open(self.out / "a.png") as im:
            self.assertEqual(im.size, (100, 100))

    def test_wrong_size_output_is_rebuilt(self):
        self._raster("a.png")
        self.out.mkdir(parents=True)
        Image.new("RGBA", (64, 64), BLUE).save(self.out / "a.png")
        self.assertEqual(self._run(), EXIT_OK)
        with Image.open(self.out / "a.png") as im:
            self.assertEqual(im.size, (100, 100))

    def test_blank_output_is_rebuilt(self):
        self._raster("a.png")
        self.out.mkdir(parents=True)
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(self.out / "a.png")
        self.assertEqual(self._run(), EXIT_OK)
        self.assertFalse(m._is_blank(Image.open(self.out / "a.png")))

    # --- source priority --------------------------------------------------

    def test_pick_sources_prefers_svg_over_rasters(self):
        self._raster("foo.png")
        Image.new("RGB", (40, 70), RED[:3]).save(self.src / "foo.jpg")  # JPEG has no alpha
        (self.src / "foo.svg").write_text(BLUE_SVG, encoding="utf-8")
        self.assertEqual([g[0].name for g in m._source_groups(self.src)], ["foo.svg"])

    def test_svg_wins_over_same_stem_png(self):
        # "foo.png" sorts before "foo.svg": lexical order used to pick the raster.
        self._raster("foo.png", RED)
        (self.src / "foo.svg").write_text(BLUE_SVG, encoding="utf-8")
        self.assertEqual(self._run(), EXIT_OK)
        self.assertEqual(len(list(self.out.glob("*.png"))), 1)
        px = self._center("foo.png")
        self.assertGreater(px[2], px[0])  # blue SVG, not the red PNG

    # --- exit codes -------------------------------------------------------

    def test_all_failed_exits_nonzero(self):
        for name in ("a.png", "b.png"):
            self._raster(name, (0, 0, 0, 0))  # blank -> never converted
        self.assertEqual(self._run(), EXIT_FAILED)
        self.assertEqual(list(self.out.glob("*.png")), [])

    def test_some_failed_exits_partial(self):
        self._raster("good.png", RED)
        self._raster("bad.png", (0, 0, 0, 0))
        self.assertEqual(self._run(), EXIT_PARTIAL)

    # --- watchdog contract ------------------------------------------------

    def test_marker_names_the_file_being_converted(self):
        """run_convert.ps1 uses the marker as its per-file heartbeat."""
        self._raster("a.png")
        marker = self.out / ".svg_cur"
        seen = []
        real = m._convert_raster

        def spy(p, out):
            seen.append(marker.read_text(encoding="utf-8"))
            return real(p, out)

        with mock.patch.object(m, "_convert_raster", spy):
            self._run()
        self.assertEqual(seen, ["a"])
        self.assertFalse(marker.exists())  # cleared once the file is done

    def test_interrupted_file_is_quarantined_and_reported(self):
        self._raster("a.png")
        self.out.mkdir(parents=True)
        (self.out / ".svg_cur").write_text("a", encoding="utf-8")  # killed mid-convert
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m._run_general(self.src, self.out, 0)
        self.assertFalse((self.out / "a.png").exists())  # not retried blindly
        self.assertIn("a", (self.out / ".svg_skip.txt").read_text(encoding="utf-8"))
        self.assertIn("REVIEW", buf.getvalue())  # never silent


if __name__ == "__main__":
    unittest.main()
