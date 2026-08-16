"""Tests for emojikit.media: detection, hashing and conversion.

Video tests require ffmpeg/ffprobe on PATH; they are skipped automatically when
those tools are unavailable.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Allow running from the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import media  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Generating a 2-second clip takes well under a second; anything near this bound
# means ffmpeg is stuck, and an unbounded child can hang the whole suite.
FFMPEG_TIMEOUT = 120
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
        media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_512.json", out)
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
        out = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_512.json",
                                    self.tmp / "c.tgs")
        media.validate_tgs(out)  # must not raise
        self.assertLessEqual(out.stat().st_size, media.TGS_MAX_BYTES)

    def test_tgs_content_key_stable(self):
        out1 = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_512.json",
                                     self.tmp / "1.tgs")
        out2 = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_512.json",
                                     self.tmp / "2.tgs")
        self.assertEqual(media.content_key(out1, "animated"),
                         media.content_key(out2, "animated"))

    def test_valid_512_canvas_is_preserved(self):
        """The canvas must survive: rewriting it to 100x100 broke the artwork."""
        out = media.to_animated_tgs(FIXTURES / "lottie" / "red_circle_512.json",
                                    self.tmp / "c.tgs")
        lottie = media._load_lottie(out)
        self.assertEqual((lottie["w"], lottie["h"]),
                         (media.TGS_SIZE, media.TGS_SIZE))

    def _tgs_from(self, lottie: dict, name: str = "x.tgs") -> Path:
        out = self.tmp / name
        with gzip.open(out, "wb") as fh:
            fh.write(json.dumps(lottie).encode("utf-8"))
        return out

    def _base_lottie(self, **over) -> dict:
        base = json.loads((FIXTURES / "lottie" / "red_circle_512.json")
                          .read_text(encoding="utf-8"))
        base.update(over)
        return base

    def test_wrong_canvas_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.validate_tgs(self._tgs_from(self._base_lottie(w=100, h=100)))

    def test_overlong_timeline_is_rejected(self):
        # 60 fps for 999 seconds: previously accepted, Telegram caps at 3 s.
        with self.assertRaises(media.MediaError):
            media.validate_tgs(self._tgs_from(self._base_lottie(op=59940)))

    def test_empty_timeline_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.validate_tgs(self._tgs_from(self._base_lottie(ip=0, op=0)))

    def test_uncompressed_json_is_not_a_tgs(self):
        raw = self.tmp / "plain.tgs"
        raw.write_text(json.dumps(self._base_lottie()), encoding="utf-8")
        with self.assertRaises(media.MediaError):
            media.validate_tgs(raw)

    def test_list_root_raises_media_error_not_attribute_error(self):
        out = self.tmp / "list.tgs"
        with gzip.open(out, "wb") as fh:
            fh.write(b"[]")
        with self.assertRaises(media.MediaError):
            media.validate_tgs(out)

    def test_decompression_is_bounded(self):
        out = self.tmp / "bomb.tgs"
        with gzip.open(out, "wb") as fh:
            fh.write(b"0" * (media.TGS_MAX_UNPACKED + 1024))
        with self.assertRaises(media.MediaError):
            media.validate_tgs(out)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not available")
class TestFingerprintMatchesTheSeparateCalls(unittest.TestCase):
    """One decode must produce EXACTLY what two decodes produced.

    Every ingest site needs both the content key and the perceptual hash, and
    computing them separately decoded each file twice -- for video, two ffmpeg
    launches over the same clip. Merging them is only safe if the values are
    bit-identical: the content key is the catalog's primary key, so a drift here
    would silently split existing entries into duplicates instead of deduping
    them.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_static_agrees_with_content_key_and_perceptual_hash(self):
        png = _make_png(self.tmp / "a.png", (200, 40, 60, 255))
        key, phash = media.fingerprint(png, "static")
        self.assertEqual(key, media.content_key(png, "static"))
        self.assertEqual(phash, media.perceptual_hash(png, "static"))
        self.assertTrue(key.startswith("s:"))
        self.assertIsNotNone(phash)

    def test_video_agrees_with_content_key_and_perceptual_hash(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        webm = media.to_video_webm(gif, self.tmp / "anim.webm")
        key, phash = media.fingerprint(webm, "video")
        self.assertEqual(key, media.content_key(webm, "video"),
                         "the merged decode changed the catalog primary key")
        self.assertEqual(phash, media.perceptual_hash(webm, "video"),
                         "frame 0 of the digest stream is not the frame the "
                         "separate call hashed")
        self.assertTrue(key.startswith("v:"))

    def test_video_runs_ffmpeg_once_not_twice(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        webm = media.to_video_webm(gif, self.tmp / "anim.webm")
        real_run, calls = media._run, []

        def counting(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)

        with mock.patch.object(media, "_run", counting):
            media.fingerprint(webm, "video")
        self.assertEqual(len(calls), 1,
                         "the whole point of fingerprint() is one ffmpeg launch")

    def test_animated_has_no_raster_hash_and_still_keys(self):
        lottie = {"v": "5.5", "w": 512, "h": 512, "fr": 60, "ip": 0, "op": 60,
                  "layers": []}
        tgs = self.tmp / "x.tgs"
        tgs.write_bytes(gzip.compress(json.dumps(lottie).encode("utf-8")))
        key, phash = media.fingerprint(tgs, "animated")
        self.assertEqual(key, media.content_key(tgs, "animated"))
        self.assertIsNone(phash)

    def test_an_unreadable_source_fails_the_same_way_it_always_did(self):
        # content_key() has always raised on an undecodable static file, and the
        # ingest sites treat that as "skip this item". Merging the two decodes
        # must not quietly turn that into a bogus key.
        bad = self.tmp / "bad.png"
        bad.write_bytes(b"not an image at all")
        with self.assertRaises(Exception) as old:
            media.content_key(bad, "static")
        with self.assertRaises(type(old.exception)):
            media.fingerprint(bad, "static")


class TestBlankDetection(unittest.TestCase):
    """The histogram form must answer exactly what the pixel loop answered."""

    def test_fully_transparent_is_blank(self):
        self.assertTrue(media.is_blank_image(
            Image.new("RGBA", (100, 100), (0, 0, 0, 0))))

    def test_a_visible_image_is_not_blank(self):
        self.assertFalse(media.is_blank_image(
            Image.new("RGBA", (100, 100), (10, 20, 30, 255))))

    def test_the_visibility_floor_is_exact(self):
        # alpha == VISIBLE_ALPHA is NOT visible; one step above it is. An
        # off-by-one in the histogram slice would flip one of these.
        at_floor = Image.new("RGBA", (100, 100), (5, 5, 5, media.VISIBLE_ALPHA))
        self.assertTrue(media.is_blank_image(at_floor))
        above = Image.new("RGBA", (100, 100), (5, 5, 5, media.VISIBLE_ALPHA + 1))
        self.assertFalse(media.is_blank_image(above))

    def test_a_few_visible_pixels_still_count_as_blank(self):
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        for i in range(media.BLANK_MAX_VISIBLE):
            img.putpixel((i, 0), (255, 255, 255, 255))
        self.assertTrue(media.is_blank_image(img))
        img.putpixel((media.BLANK_MAX_VISIBLE, 0), (255, 255, 255, 255))
        self.assertFalse(media.is_blank_image(img))


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

    # --- validation must see what Telegram actually constrains -------------
    def _lavfi(self, name, *extra, size="100x100", rate=30, duration=2):
        out = self.tmp / name
        subprocess.run(
            [media.ffmpeg_path(), "-y", "-f", "lavfi", "-i",
             f"testsrc2=size={size}:rate={rate}:duration={duration}", *extra,
             str(out)], capture_output=True, check=True, timeout=FFMPEG_TIMEOUT)
        return out

    VP9 = ("-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "50",
           "-b:v", "0")

    def test_compliant_webm_is_accepted(self):
        media.validate_video(self._lavfi("ok.webm", *self.VP9, "-an"))

    def test_audio_stream_is_rejected(self):
        """Telegram video emoji must carry no audio.

        probe_video used to select only stream v:0, so an audio track was
        invisible to the validator.
        """
        out = self.tmp / "audio.webm"
        subprocess.run(
            [media.ffmpeg_path(), "-y",
             "-f", "lavfi", "-i", "testsrc2=size=100x100:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             *self.VP9, "-c:a", "libopus", "-shortest", str(out)],
            capture_output=True, check=True, timeout=FFMPEG_TIMEOUT)
        with self.assertRaises(media.MediaError) as cm:
            media.validate_video(out)
        self.assertIn("audio", str(cm.exception))

    def test_excessive_frame_rate_is_rejected(self):
        out = self._lavfi("fast.webm", *self.VP9, "-an", rate=60)
        with self.assertRaises(media.MediaError) as cm:
            media.validate_video(out)
        self.assertIn("fps", str(cm.exception))

    def test_wrong_codec_is_rejected(self):
        out = self._lavfi("vp8.webm", "-c:v", "libvpx", "-crf", "50",
                          "-b:v", "0", "-an")
        with self.assertRaises(media.MediaError):
            media.validate_video(out)

    def test_wrong_container_is_rejected(self):
        out = self._lavfi("x.mp4", *self.VP9, "-an", "-f", "mp4")
        with self.assertRaises(media.MediaError) as cm:
            media.validate_video(out)
        self.assertIn("container", str(cm.exception))

    def test_wrong_dimensions_are_rejected(self):
        out = self._lavfi("big.webm", *self.VP9, "-an", size="512x512")
        with self.assertRaises(media.MediaError):
            media.validate_video(out)


if __name__ == "__main__":
    unittest.main()
