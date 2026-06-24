"""Tests for emojikit.media: detection, hashing and conversion.

Video tests require ffmpeg/ffprobe on PATH; they are skipped automatically when
those tools are unavailable.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running from the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import media  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_png(path: Path, color, size=(80, 60), fmt="PNG") -> Path:
    img = Image.new("RGBA", size, color)
    img.save(path, format=fmt)
    return path


def _make_anim_gif(path: Path, size=(64, 64), frames=6) -> Path:
    imgs = []
    for i in range(frames):
        im = Image.new("RGB", size, (10 * i % 255, 50, 200))
        imgs.append(im)
    imgs[0].save(path, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=80, loop=0)
    return path


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_png_is_static(self):
        p = _make_png(self.tmp / "a.png", (0, 128, 255, 255))
        self.assertEqual(media.detect_format(p), "static")

    def test_webp_is_static(self):
        p = _make_png(self.tmp / "a.webp", (0, 128, 255, 255), fmt="WEBP")
        self.assertEqual(media.detect_format(p), "static")

    def test_tgs_is_animated(self):
        out = self.tmp / "a.tgs"
        media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_100.json", out)
        self.assertEqual(media.detect_format(out), "animated")

    def test_sticker_format_mapping(self):
        self.assertEqual(media.telegram_sticker_format({"is_video": True}), "video")
        self.assertEqual(media.telegram_sticker_format({"is_animated": True}), "animated")
        self.assertEqual(media.telegram_sticker_format({}), "static")


class TestStaticHashing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_content_same_key(self):
        # Same pixels saved twice (different files) -> identical content key.
        a = _make_png(self.tmp / "a.png", (200, 30, 30, 255))
        b = _make_png(self.tmp / "b.png", (200, 30, 30, 255))
        self.assertEqual(media.content_key(a, "static"),
                         media.content_key(b, "static"))

    def test_different_content_different_key(self):
        a = _make_png(self.tmp / "a.png", (200, 30, 30, 255))
        c = _make_png(self.tmp / "c.png", (30, 200, 30, 255))
        self.assertNotEqual(media.content_key(a, "static"),
                            media.content_key(c, "static"))

    def test_to_static_png_is_100(self):
        src = _make_png(self.tmp / "s.png", (10, 20, 30, 255), size=(40, 90))
        out = media.to_static_png(src, self.tmp / "out.png")
        with Image.open(out) as im:
            self.assertEqual(im.size, (100, 100))

    def test_phash_near_duplicate(self):
        a = _make_png(self.tmp / "a.png", (120, 120, 120, 255), size=(64, 64))
        # Re-encode as webp (lossless) -> visually identical, near-zero distance.
        b = self.tmp / "b.webp"
        Image.open(a).save(b, format="WEBP", lossless=True)
        ha, hb = media.perceptual_hash(a, "static"), media.perceptual_hash(b, "static")
        self.assertIsNotNone(ha)
        self.assertLessEqual(media.hamming(ha, hb), 5)


class TestAnimated(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tgs_roundtrip_valid(self):
        out = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_100.json",
                                    self.tmp / "c.tgs")
        media.validate_tgs(out)  # must not raise
        self.assertLessEqual(out.stat().st_size, media.TGS_MAX_BYTES)

    def test_tgs_content_key_stable(self):
        out1 = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_100.json",
                                     self.tmp / "1.tgs")
        out2 = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_100.json",
                                     self.tmp / "2.tgs")
        self.assertEqual(media.content_key(out1, "animated"),
                         media.content_key(out2, "animated"))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not available")
class TestVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gif_to_webm_valid(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        out = media.to_video_webm(gif, self.tmp / "anim.webm")
        media.validate_video(out)  # 100x100, <=3s, vp9, <=256KB
        self.assertEqual(media.detect_format(out), "video")

    def test_video_content_key(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        out = media.to_video_webm(gif, self.tmp / "anim.webm")
        key = media.content_key(out, "video")
        self.assertTrue(key.startswith("v:"))


if __name__ == "__main__":
    unittest.main()
