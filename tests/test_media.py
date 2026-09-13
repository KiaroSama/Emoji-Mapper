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

from PIL import Image  # noqa: E402

from emojikit import identity, media, video_decode  # noqa: E402

from tests._media_fixtures import (FFMPEG_TIMEOUT, HAS_FFMPEG,  # noqa: E402
                                   _clear_pixels, _ffmpeg_calls,
                                   _make_anim_gif, _make_png)

FIXTURES = Path(__file__).resolve().parent / "fixtures"




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

    def test_only_a_real_video_input_names_the_alpha_decoder(self):
        """A GIF or PNG input must not be handed a vp9 decoder.

        The decoder is chosen from the PROBED codec, never the extension -- a
        `.webm` is a container and says nothing about what is inside it -- so
        the encode is now preceded by an ffprobe. Only the ffmpeg calls are
        the subject here; `_ffmpeg_calls` drops the probe.
        """
        seen = []

        def fake_run(cmd, *a, **kw):
            seen.append(cmd)
            (self.tmp / "o.webm").write_bytes(b"x")     # tiny -> loop stops

        # Real inputs, because the decoder is decided by probing them -- and
        # the probe has to happen for real, before `_run` is replaced. It is
        # memoised per file, so the mocked encode below reuses this answer
        # instead of asking a stub that cannot reply.
        gif = _make_anim_gif(self.tmp / "a.gif")
        webm = media.to_video_webm(gif, self.tmp / "src.webm")
        cmds = {}
        for name, src in (("webm", webm), ("gif", gif)):
            self.assertEqual(video_decode.decoder_args(src),
                             ["-c:v", "libvpx-vp9"] if name == "webm" else [])
            seen.clear()
            with mock.patch.object(media, "_run", fake_run):
                media.to_video_webm(src, self.tmp / "o.webm")
            cmds[name] = _ffmpeg_calls(seen)[-1]
        webm_cmd, gif_cmd = cmds["webm"], cmds["gif"]
        self.assertIn("libvpx-vp9", webm_cmd[:webm_cmd.index("-i")])
        self.assertNotIn("libvpx-vp9", gif_cmd[:gif_cmd.index("-i")])

    def test_video_decodes_once_not_twice(self):
        """One DECODE, which is the expensive half.

        The metadata probe that picks the decoder is an ffprobe: it reads the
        header and stops, and without it the decoder would be guessed from the
        extension -- the defect that made two clips differing only in opacity
        share one content key. What must not double is the frame decode.
        """
        gif = _make_anim_gif(self.tmp / "anim.gif")
        webm = media.to_video_webm(gif, self.tmp / "anim.webm")
        real_run, calls = media._run, []

        def counting(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)

        with mock.patch.object(media, "_run", counting):
            identity.fingerprint(webm, "video")
        self.assertEqual(len(_ffmpeg_calls(calls)), 1,
                         "the whole point of fingerprint() is one decode")

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
