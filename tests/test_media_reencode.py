"""Re-encoding: we publish our own bytes, and only ours.

Owner rule 1 -- never republish another pack's file byte-for-byte -- lives in
`media.reencode_in_place`, and it pulls against a second requirement that is
just as load-bearing: the result must be pixel-identical, because the content
key is computed from decoded pixels and a lossy re-compress would split one
catalog row into two. Split out of `test_media.py`, which was past the size
ceiling; detection, hashing and the fingerprint contract stay there.

Video tests require ffmpeg/ffprobe on PATH; they are skipped automatically when
those tools are unavailable.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from tests._media_fixtures import FFMPEG_TIMEOUT, _make_png

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import identity, media  # noqa: E402


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


class AnUnderDeclaredVideoKeepsItsOriginalBytes(unittest.TestCase):
    """A published sticker can claim to be shorter than it is.

    One served by Telegram carried a 3.000 s container header over 3.916 s of
    frames. Telegram's uploader reads the header, so the original was accepted
    while our truthful remux -- which recomputes duration from the packets --
    came back STICKER_VIDEO_LONG. Keeping the original is the same trade-off
    the size cap already makes: a byte-clone beats an upload that cannot happen.
    """

    def test_a_remux_over_the_duration_cap_keeps_the_original(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "v.webm"
            src.write_bytes(b"original-bytes")
            before = src.read_bytes()
            long = media.VideoInfo(width=100, height=100,
                                   duration=media.WEBM_MAX_SECONDS + 0.9,
                                   codec="vp9")
            with mock.patch.object(media, "_run"),                  mock.patch.object(media, "probe_video", return_value=long),                  mock.patch.object(Path, "read_bytes", autospec=True,
                              side_effect=lambda self: before):
                self.assertFalse(media.reencode_in_place(src, "video"))
            self.assertEqual(src.read_bytes(), before, "the original must survive")

    def test_a_remux_inside_the_cap_still_rewrites(self):
        """The guard must not turn every video into a byte-clone."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "v.webm"
            src.write_bytes(b"original-bytes")
            ok = media.VideoInfo(width=100, height=100,
                                 duration=media.WEBM_MAX_SECONDS - 0.5,
                                 codec="vp9")
            seen = iter([b"original-bytes", b"remuxed-bytes"])
            with mock.patch.object(media, "_run"),                  mock.patch.object(media, "probe_video", return_value=ok),                  mock.patch.object(Path, "read_bytes", autospec=True,
                              side_effect=lambda self: next(seen)):
                self.assertTrue(media.reencode_in_place(src, "video"))
            self.assertEqual(src.read_bytes(), b"remuxed-bytes")


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
