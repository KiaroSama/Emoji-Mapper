"""Tests for emojikit.media: detection, hashing and conversion.

Video tests require ffmpeg/ffprobe on PATH; they are skipped automatically when
those tools are unavailable.
"""

from __future__ import annotations

import copy
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

from PIL import Image, ImageDraw  # noqa: E402

from emojikit import identity, media  # noqa: E402

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



def _clear_pixels(webm: Path) -> int:
    """Fully transparent pixels in frame 0, read with the ALPHA-AWARE decoder.

    ffmpeg's default vp9 decoder silently drops the separate alpha layer, so a
    naive probe reports every VP9 emoji as opaque -- including the ones that are
    fine. Measuring with the wrong decoder is how this bug hid.
    """
    raw = subprocess.run(
        [media.ffmpeg_path(), "-v", "error", "-c:v", "libvpx-vp9", "-i", str(webm),
         "-frames:v", "1", "-vf", "format=rgba", "-f", "rawvideo",
         "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    return sum(1 for i in range(3, len(raw), 4) if raw[i] == 0)

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

    def test_rgb_under_transparent_pixels_cannot_move_the_hash(self):
        """convert("L") on RGBA DISCARDS alpha and reads the raw RGB.

        RGB beneath a fully transparent pixel is undefined and every encoder
        rewrites it -- the same trap owner rule 1 meets with ``exact=True``.
        A real logo measured 10 dHash bits from Telegram's re-encode of ITSELF
        (tolerance 6) with a byte-identical alpha channel, which failed the
        upload check and stranded a published sticker.
        """
        shape = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(10, 40):
            for y in range(8, 52):
                shape.putpixel((x, y), (30, 160, 220, 255))
        # Same picture, different garbage under the invisible pixels.
        other = shape.copy()
        for x in range(64):
            for y in range(64):
                if other.getpixel((x, y))[3] == 0:
                    other.putpixel((x, y), ((x * 7) % 256, (y * 13) % 256, 90, 0))

        self.assertEqual(identity._dhash(shape), identity._dhash(other),
                         "invisible pixels must not reach the hash")

    def test_the_hash_still_separates_genuinely_different_art(self):
        """The alpha fix must not flatten everything into one hash."""
        a = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(4, 30):
            for y in range(4, 60):
                a.putpixel((x, y), (200, 40, 40, 255))
        b = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(34, 60):
            for y in range(4, 60):
                b.putpixel((x, y), (200, 40, 40, 255))
        self.assertGreater(identity.hamming(identity._dhash(a), identity._dhash(b)), 6,
                           "a bar on the left and a bar on the right are not the same")

    def test_an_opaque_image_hashes_exactly_as_before(self):
        """Premultiplying by 255 is the identity, so nothing opaque moved."""
        im = Image.new("RGB", (40, 40), (10, 90, 200))
        for x in range(0, 40, 3):
            for y in range(40):
                im.putpixel((x, y), (240, 240, 10))
        self.assertEqual(identity._dhash(im), identity._dhash(im.convert("RGBA")))

    def test_identical_content_same_key(self):
        # Same pixels saved twice (different files) -> identical content key.
        a = _make_png(self.tmp / "a.png", (200, 30, 30, 255))
        b = _make_png(self.tmp / "b.png", (200, 30, 30, 255))
        self.assertEqual(identity.content_key(a, "static"),
                         identity.content_key(b, "static"))

    def test_different_content_different_key(self):
        a = _make_png(self.tmp / "a.png", (200, 30, 30, 255))
        c = _make_png(self.tmp / "c.png", (30, 200, 30, 255))
        self.assertNotEqual(identity.content_key(a, "static"),
                            identity.content_key(c, "static"))

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
        ha, hb = identity.perceptual_hash(a, "static"), identity.perceptual_hash(b, "static")
        self.assertIsNotNone(ha)
        self.assertLessEqual(identity.hamming(ha, hb), 5)


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
        self.assertEqual(identity.content_key(out1, "animated"),
                         identity.content_key(out2, "animated"))

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
class SubtractMasksAreRefusedBeforeTelegramRefusesThem(unittest.TestCase):
    """Telegram's UPLOADER rejects a subtract mask; its player does not.

    A real sticker proved it: downloaded from a live pack, sent straight back
    untouched, and refused with "Bad Request: wrong file type". Nothing local
    could see it -- 512x512, 30 fps, 2 s, valid gzip, valid JSON -- so it entered
    the catalog and only failed deep inside a publish, where the message names
    neither the item nor the reason.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lottie = json.loads(
            (FIXTURES / "lottie" / "red_circle_512.json").read_text(encoding="utf-8"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tgs(self, doc) -> Path:
        out = self.tmp / "x.tgs"
        raw = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        with open(out, "wb") as fh:
            with gzip.GzipFile(filename="", fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(raw)
        return out

    def _masked(self, mode: str, *, inside_precomp: bool):
        doc = copy.deepcopy(self.lottie)
        layer = {"ty": 4, "nm": "masked", "ip": 0, "op": 10,
                 "masksProperties": [{"mode": mode, "nm": "m"}]}
        if inside_precomp:
            doc["assets"] = [{"id": "comp_0", "layers": [layer]}]
            doc["layers"] = [{"ty": 0, "nm": "precomp", "refId": "comp_0",
                              "ip": 0, "op": 10}]
        else:
            doc["layers"] = list(doc.get("layers") or []) + [layer]
        return self._tgs(doc)

    def test_a_subtract_mask_is_rejected(self):
        with self.assertRaises(media.MediaError) as cm:
            media.validate_tgs(self._masked("s", inside_precomp=False))
        self.assertIn("SUBTRACT", str(cm.exception))

    def test_a_subtract_mask_INSIDE_A_PRECOMP_is_rejected(self):
        """Where the real one was hiding.

        A top-level scan sees one innocent precomp layer and passes the file.
        """
        with self.assertRaises(media.MediaError) as cm:
            media.validate_tgs(self._masked("s", inside_precomp=True))
        self.assertIn("SUBTRACT", str(cm.exception))

    def test_an_ADD_mask_is_still_allowed(self):
        """Two accepted stickers use add masks: refusing those loses real work."""
        media.validate_tgs(self._masked("a", inside_precomp=True))
        media.validate_tgs(self._masked("a", inside_precomp=False))


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
        key, phash = identity.fingerprint(png, "static")
        self.assertEqual(key, identity.content_key(png, "static"))
        self.assertEqual(phash, identity.perceptual_hash(png, "static"))
        self.assertTrue(key.startswith("s:"))
        self.assertIsNotNone(phash)

    def test_video_agrees_with_content_key_and_perceptual_hash(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        webm = media.to_video_webm(gif, self.tmp / "anim.webm")
        key, phash = identity.fingerprint(webm, "video")
        self.assertEqual(key, identity.content_key(webm, "video"),
                         "the merged decode changed the catalog primary key")
        self.assertEqual(phash, identity.perceptual_hash(webm, "video"),
                         "frame 0 of the digest stream is not the frame the "
                         "separate call hashed")
        self.assertTrue(key.startswith("v:"))

    def test_a_transparent_webm_survives_a_re_encode(self):
        """VP9 keeps alpha in a SEPARATE layer that the default decoder drops.

        Without naming the libvpx decoder on the way IN, the filter chain never
        sees an alpha channel and the transparent pad lands on an opaque frame:
        a cue-ball emoji came out a black square. Every video emoji until then
        had arrived as a download that owner rule 1 remuxes with `-c copy`, so
        this path had never re-encoded a transparent source.
        """
        # NON-SQUARE on purpose: the 100x100 output pads a 2:1 frame with
        # transparent bars, and those bars are exactly what got flattened.
        # A square source scales edge to edge and would pass either way.
        png = _make_png(self.tmp / "dot.png", (10, 200, 90, 255), size=(80, 40))
        src = media.to_video_webm(png, self.tmp / "src.webm")
        again = media.to_video_webm(src, self.tmp / "again.webm")
        self.assertGreater(_clear_pixels(again), 0,
                           "re-encoding a transparent webm flattened it")

    def test_only_a_webm_input_names_the_alpha_decoder(self):
        """A GIF or PNG input must not be handed a vp9 decoder."""
        seen = []

        def fake_run(cmd, *a, **kw):
            seen.append(cmd)
            (self.tmp / "o.webm").write_bytes(b"x")     # tiny -> loop stops

        with mock.patch.object(media, "_run", fake_run):
            for name in ("a.webm", "a.gif"):
                media.to_video_webm(self.tmp / name, self.tmp / "o.webm")
        webm_cmd, gif_cmd = seen[0], seen[-1]
        self.assertIn("libvpx-vp9", webm_cmd[:webm_cmd.index("-i")])
        self.assertNotIn("libvpx-vp9", gif_cmd[:gif_cmd.index("-i")])

    def test_video_runs_ffmpeg_once_not_twice(self):
        gif = _make_anim_gif(self.tmp / "anim.gif")
        webm = media.to_video_webm(gif, self.tmp / "anim.webm")
        real_run, calls = media._run, []

        def counting(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)

        with mock.patch.object(media, "_run", counting):
            identity.fingerprint(webm, "video")
        self.assertEqual(len(calls), 1,
                         "the whole point of fingerprint() is one ffmpeg launch")

    def test_animated_has_no_raster_hash_and_still_keys(self):
        lottie = {"v": "5.5", "w": 512, "h": 512, "fr": 60, "ip": 0, "op": 60,
                  "layers": []}
        tgs = self.tmp / "x.tgs"
        tgs.write_bytes(gzip.compress(json.dumps(lottie).encode("utf-8")))
        key, phash = identity.fingerprint(tgs, "animated")
        self.assertEqual(key, identity.content_key(tgs, "animated"))
        self.assertIsNone(phash)

    def test_an_unreadable_source_fails_the_same_way_it_always_did(self):
        # content_key() has always raised on an undecodable static file, and the
        # ingest sites treat that as "skip this item". Merging the two decodes
        # must not quietly turn that into a bogus key.
        bad = self.tmp / "bad.png"
        bad.write_bytes(b"not an image at all")
        with self.assertRaises(Exception) as old:
            identity.content_key(bad, "static")
        with self.assertRaises(type(old.exception)):
            identity.fingerprint(bad, "static")


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
        key = identity.content_key(out, "video")
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


class TestReencodeGivesUsOurOwnBytes(unittest.TestCase):
    """Owner rule: never republish another pack's file byte-for-byte.

    Both halves matter and they pull against each other, so both are asserted:
    the BYTES must change (or we published a clone) and the PICTURE must not
    (or we degraded someone's logo to win a hash).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _webp_with_transparency(path: Path) -> Path:
        """A sticker-shaped source: a coloured blob on transparency.

        The transparent region is the point. libwebp's lossless mode rewrites
        the RGB *under* fully transparent pixels unless exact=True, so a
        fully-opaque fixture cannot catch that -- a real .webp sticker did.
        """
        im = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
        for x in range(20, 60):
            for y in range(15, 45):
                im.putpixel((x, y), (200, 40, 90, 255))
        im.putpixel((2, 2), (7, 9, 11, 0))     # colour hiding under alpha=0
        im.save(path, format="WEBP", lossless=True, exact=True)
        return path

    def test_static_bytes_change_and_pixels_do_not(self):
        src = self._webp_with_transparency(self.dir / "t.webp")
        before_bytes = src.read_bytes()
        before_px = Image.open(src).convert("RGBA").get_flattened_data()

        self.assertTrue(media.reencode_in_place(src, "static"))
        self.assertNotEqual(src.read_bytes(), before_bytes,
                            "the republished file is a byte-clone of the source")
        self.assertEqual(Image.open(src).convert("RGBA").get_flattened_data(),
                         before_px,
                         "re-encoding changed the picture, including the RGB "
                         "under transparent pixels; it must be pixel-exact")

    def test_static_opaque_bytes_change_and_pixels_do_not(self):
        src = _make_png(self.dir / "a.webp", (200, 40, 90, 255), fmt="WEBP")
        before_bytes = src.read_bytes()
        # get_flattened_data, not getdata: the latter is removed in Pillow 14.
        before_px = Image.open(src).convert("RGBA").get_flattened_data()

        self.assertTrue(media.reencode_in_place(src, "static"))
        self.assertNotEqual(src.read_bytes(), before_bytes,
                            "the republished file is a byte-clone of the source")
        self.assertEqual(Image.open(src).convert("RGBA").get_flattened_data(),
                         before_px,
                         "re-encoding changed the picture; it must be pixel-exact")

    def test_the_content_key_survives_so_dedup_still_works(self):
        """content_key hashes normalized pixels, not container bytes."""
        src = _make_png(self.dir / "b.webp", (10, 180, 60, 255), fmt="WEBP")
        before = identity.content_key(src, "static")
        media.reencode_in_place(src, "static")
        self.assertEqual(identity.content_key(src, "static"), before,
                         "re-encoding moved the catalog's dedup key")

    def test_animated_tgs_regzips_to_the_same_animation(self):
        lottie = {"v": "5.5", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
                  "layers": []}
        src = self.dir / "c.tgs"
        src.write_bytes(gzip.compress(json.dumps(lottie).encode("utf-8")))
        # A different gzip level, so the source is not already our own output.
        before_bytes = src.read_bytes()

        media.reencode_in_place(src, "animated")
        self.assertEqual(media._load_lottie(src), lottie,
                         "the animation itself changed")
        self.assertNotEqual(src.read_bytes(), before_bytes)

    def test_regzip_is_deterministic(self):
        """Two runs must agree, or ingest would see a new file every time."""
        lottie = {"v": "5.5", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
                  "layers": []}
        outs = []
        for n in ("d1.tgs", "d2.tgs"):
            p = self.dir / n
            p.write_bytes(gzip.compress(json.dumps(lottie).encode("utf-8"), 1))
            media.reencode_in_place(p, "animated")
            outs.append(p.read_bytes())
        self.assertEqual(outs[0], outs[1],
                         "a timestamp in the gzip header would make every "
                         "re-run look like a different file")

    def test_an_unreadable_file_is_reported_not_raised(self):
        """One bad sticker must not stop a whole pack ingest."""
        bad = self.dir / "e.webp"
        bad.write_bytes(b"not an image at all")
        self.assertFalse(media.reencode_in_place(bad, "static"))
        self.assertEqual(bad.read_bytes(), b"not an image at all",
                         "a failed re-encode must leave the file alone")

    def test_growing_past_a_format_cap_keeps_the_original(self):
        """Lossless can grow a file, and Telegram's caps are hard.

        A real .tgs measured 64 139 bytes after re-encoding against a 65 536
        cap, so a source already near the limit can cross it. A byte-clone is a
        lesser failure than an upload Telegram rejects.
        """
        lottie = {"v": "5.5", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
                  "layers": []}
        src = self.dir / "big.tgs"
        src.write_bytes(gzip.compress(json.dumps(lottie).encode("utf-8"), 1))
        before = src.read_bytes()
        with mock.patch.object(media, "TGS_MAX_BYTES", 1):   # cap below any output
            self.assertFalse(media.reencode_in_place(src, "animated"))
        self.assertEqual(src.read_bytes(), before,
                         "the file was rewritten past its own format cap")

    def test_an_unknown_format_is_left_alone(self):
        p = self.dir / "f.bin"
        p.write_bytes(b"\x00\x01\x02")
        self.assertFalse(media.reencode_in_place(p, "sticker-shaped-thing"))
        self.assertEqual(p.read_bytes(), b"\x00\x01\x02")


class TestSingleFrameVideoKeepsAContentKey(unittest.TestCase):
    """A one-frame video's key must describe its PICTURE, not its bytes.

    The digest resamples to a fixed 10 fps so two encodes of the same clip
    agree. A single-frame video is shorter than one sampling interval, so the
    filter emitted nothing and the digest fell through to hashing the container
    bytes. Consequences, both real and both found in the collector catalog:
    two such stickers differing only in container framing did not dedup, and
    re-encoding one under owner rule 1 moved its primary key, orphaning its
    catalog row and its media path -- which are both named after that key.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _one_frame_webm(self, name: str) -> Path:
        out = self.dir / name
        subprocess.run(
            [media.ffmpeg_path(), "-y", "-f", "lavfi", "-i",
             "testsrc2=size=100x100:rate=30:duration=1", "-frames:v", "1",
             "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "50",
             "-b:v", "0", "-an", str(out)],
            capture_output=True, check=True, timeout=FFMPEG_TIMEOUT)
        return out

    def test_the_key_survives_a_reencode(self):
        src = self._one_frame_webm("one.webm")
        before_bytes = src.read_bytes()
        key_before = identity.content_key(src, "video")

        self.assertTrue(media.reencode_in_place(src, "video"),
                        "nothing was rewritten, so this proves nothing")
        self.assertNotEqual(src.read_bytes(), before_bytes,
                            "owner rule 1: our bytes must differ from theirs")
        self.assertEqual(identity.content_key(src, "video"), key_before,
                         "the primary key moved on a -c copy remux, which "
                         "orphans the catalog row and the media path")

    def test_fingerprint_and_content_key_agree(self):
        """They must be the same key, or ingest and dedup disagree.

        fingerprint() carried its own copy of the ffmpeg call. When the
        single-frame retry was added to the digest alone, the two produced
        DIFFERENT keys for the same file -- and fingerprint()'s byte-hash
        fallback hashed a WebM whose SegmentUID is random, so the "identity" of
        a single-frame video changed on every run.
        """
        src = self._one_frame_webm("agree.webm")
        key, _phash = identity.fingerprint(src, "video")
        self.assertEqual(key, identity.content_key(src, "video"))

    def test_the_key_is_stable_across_runs(self):
        src = self._one_frame_webm("stable.webm")
        first = identity.content_key(src, "video")
        self.assertEqual(first, identity.content_key(src, "video"))
        media.reencode_in_place(src, "video")
        self.assertEqual(identity.content_key(src, "video"), first,
                         "a remux rewrites the container's random SegmentUID; "
                         "a key that follows it is a byte hash, not identity")

    def test_the_key_is_not_merely_a_hash_of_the_file(self):
        src = self._one_frame_webm("a.webm")
        clone = self.dir / "b.webm"
        # Same single frame, different container bytes -- exactly the pair the
        # byte-hash fallback failed to collapse.
        subprocess.run([media.ffmpeg_path(), "-y", "-i", str(src), "-c", "copy",
                        str(clone)], capture_output=True, check=True,
                       timeout=FFMPEG_TIMEOUT)
        self.assertNotEqual(src.read_bytes(), clone.read_bytes(),
                            "the two files are byte-identical; nothing tested")
        self.assertEqual(identity.content_key(src, "video"),
                         identity.content_key(clone, "video"),
                         "two encodes of one frame must dedup onto one key")


class SameImageSurvivesAReEncode(unittest.TestCase):
    """`content_key` equality cannot answer "is this our upload?".

    The key is a SHA of exact pixels, so Telegram's lossy re-encode changes it
    for a picture that is visually identical. The publisher read that as proof
    of a FOREIGN sticker and stopped a 449-emoji run on one that had landed
    correctly, so a mismatch must not be a negative on its own.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _art(self, name, seed=0):
        """A mark with smooth shapes, like the logos this actually ships."""
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse([8, 8, 92, 92], fill=(20 + seed * 90, 120, 220 - seed * 60, 255))
        dr.rounded_rectangle([30 - seed * 12, 34, 70, 66 + seed * 14],
                             radius=8, fill=(255, 255, 255, 255))
        p = self.d / name
        img.save(p, format="PNG")
        return p

    def _lossy(self, src, name, quality=90):
        """A lossy re-encode that keeps RGB under transparent pixels.

        ``exact=True`` is not decoration here, it is what makes this a model of
        Telegram rather than of a different bug. Without it libwebp rewrites the
        colour beneath fully transparent pixels -- owner rule 1's trap -- and
        the dHash moves 12 bits on a picture that looks untouched, against the
        0..3 measured on real round-tripped stickers. A fixture drifting four
        times further than the thing it stands for would be testing the
        tolerance against a fiction.
        """
        p = self.d / name
        Image.open(src).convert("RGBA").save(p, format="WEBP",
                                             quality=quality, exact=True)
        return p

    def test_the_exact_key_really_does_move(self):
        """If it did not, this whole function would be unnecessary."""
        src = self._art("a.png")
        enc = self._lossy(src, "a.webp")
        self.assertNotEqual(identity.content_key(src, "static"),
                            identity.content_key(enc, "static"),
                            "no drift: the fixture cannot exercise the bug")

    def test_a_re_encode_of_the_same_picture_is_the_same_picture(self):
        src = self._art("a.png")
        self.assertIs(identity.same_image(self._lossy(src, "a.webp"), src, "static"),
                      True)

    def test_an_identical_file_takes_the_exact_path(self):
        src = self._art("a.png")
        self.assertIs(identity.same_image(src, src, "static"), True)

    def test_a_different_picture_is_still_rejected(self):
        """The tolerance must not have swallowed the guard it replaced."""
        a, b = self._art("a.png", seed=0), self._art("b.png", seed=9)
        self.assertIs(identity.same_image(self._lossy(a, "a.webp"), b, "static"),
                      False)

    def test_animated_cannot_be_decided_and_says_so(self):
        """Vector has no raster hash -- and unknown is not false.

        Answering False here would accuse a live sticker of being a stranger on
        the strength of a comparison that was never made.
        """
        a, b = self._art("a.png", seed=0), self._art("b.png", seed=9)
        self.assertIsNone(identity.same_image(a, b, "animated"))

    def test_the_same_shape_in_another_colour_is_not_our_upload(self):
        """dHash is grayscale, so structure alone cannot answer this.

        A red square and a stranger's green square of the same shape sit 4 bits
        apart -- inside any tolerance loose enough to survive the re-encode. A
        structure-only check therefore reports a foreign sticker as ours, which
        is how an item gets marked done against someone else's emoji. Colour is
        the second, independent signal that catches it.

        Not a hypothetical: accepting this pair is exactly the regression that
        `test_a_foreign_sticker_landing_is_not_read_as_our_upload` caught.
        """
        def square(name, colour):
            img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle([20, 20, 79, 79], fill=colour)
            p = self.d / name
            img.save(p, format="PNG")
            return p

        ours = square("ours.png", (200, 30, 30, 255))
        theirs = square("theirs.png", (10, 200, 40, 255))
        self.assertLessEqual(
            identity.hamming(identity.perceptual_hash(ours, "static"),
                          identity.perceptual_hash(theirs, "static")),
            identity.UPLOAD_PHASH_TOLERANCE,
            "fixture no longer exercises the hole: structure alone must accept these")
        self.assertIs(identity.same_image(ours, theirs, "static"), False)

    def test_an_unreadable_file_is_undecidable_not_negative(self):
        good = self._art("a.png")
        bad = self.d / "torn.png"
        bad.write_bytes(b"not an image")
        self.assertIsNone(identity.same_image(bad, good, "static"))


if __name__ == "__main__":
    unittest.main()
