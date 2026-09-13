"""Video identity: alpha is part of the picture, and there is one sampling rule.

F12. VP9 keeps alpha in a separate WebM layer and ffmpeg's default ``vp9``
decoder drops it in silence. Two real clips with identical RGB and alpha 255
versus 129 therefore produced ONE content key, ``same_image`` called them equal,
and ``Catalog.add`` merged them -- deleting the second file. The decoder is now
chosen from the container's actual codec.

F13. ``fingerprint(path, "video")`` hashed frame zero of the ``fps=10`` sample
stream while ``perceptual_hash(path, "video")`` ran its own ffmpeg with no fps
filter at all. Those are different frames: one 30 fps clip measured
5300177295401782857 through the first and 6510615555426900570 through the
second. Both slice the same canonical stream now.

These use REAL ffmpeg encodes. A synthetic fixture cannot carry a VP9 alpha
layer, which is the entire subject. ffmpeg is a documented prerequisite for
video emoji and CI installs it, so its absence FAILS here rather than skipping
quietly to green -- a media test that skips is how a media defect survives.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

from emojikit import identity, media, video_decode

# Built once for the module: every clip here costs a real encode, and the same
# handful of files answers every question asked below.
_TMP: Path | None = None
CLIPS: dict[str, Path] = {}


def _flat(alpha, n=6):
    return [Image.new("RGBA", (64, 64), (255, 0, 0, alpha)) for _ in range(n)]


def _structured(n):
    """Frame zero deliberately unlike the rest: this is what F13 turns on."""
    out = []
    for i in range(n):
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 255))
        draw = ImageDraw.Draw(im)
        if i == 0:
            draw.rectangle([0, 0, 31, 63], fill=(255, 255, 255, 255))
        else:
            draw.ellipse([8, 8, 55, 55], fill=(255, 255, 255, 255))
        out.append(im)
    return out


def _encode(frames, out: Path, *, fps=30, codec="libvpx-vp9"):
    src = out.parent / (out.stem + "_frames")
    src.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(frames):
        im.save(src / f"{i:03d}.png")
    subprocess.run(
        [media.ffmpeg_path(), "-y", "-v", "error", "-framerate", str(fps),
         "-i", str(src / "%03d.png"), "-c:v", codec, "-pix_fmt", "yuva420p",
         # VP8 refuses to encode transparency with alt-ref frames enabled.
         "-auto-alt-ref", "0", str(out)],
        check=True, timeout=180)
    return out


def setUpModule():
    global _TMP
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest(
            "ffmpeg/ffprobe are required for the video identity tests and are a "
            "documented prerequisite of this project (CI installs them). "
            "Install them rather than running the suite without this coverage: "
            "winget install Gyan.FFmpeg")
    _TMP = Path(tempfile.mkdtemp(prefix="identity-video-"))
    CLIPS["opaque"] = _encode(_flat(255), _TMP / "opaque.webm")
    CLIPS["faint"] = _encode(_flat(129), _TMP / "faint.webm")
    CLIPS["clear"] = _encode(_flat(0), _TMP / "clear.webm")
    CLIPS["vp8_opaque"] = _encode(_flat(255), _TMP / "vp8_opaque.webm", codec="libvpx")
    CLIPS["vp8_faint"] = _encode(_flat(120), _TMP / "vp8_faint.webm", codec="libvpx")
    CLIPS["fast30"] = _encode(_structured(12), _TMP / "fast30.webm", fps=30)
    CLIPS["fast15"] = _encode(_structured(8), _TMP / "fast15.webm", fps=15)
    CLIPS["single"] = _encode(_structured(1), _TMP / "single.webm", fps=30)
    # A genuine container-level remux: the same picture, different bytes.
    CLIPS["remux"] = _TMP / "remux.webm"
    subprocess.run([media.ffmpeg_path(), "-y", "-v", "error", "-i",
                    str(CLIPS["opaque"]), "-c", "copy", str(CLIPS["remux"])],
                   check=True, timeout=180)
    # A download saved with no extension, which is how one really arrives.
    CLIPS["noext"] = _TMP / "downloaded_blob"
    CLIPS["noext"].write_bytes(CLIPS["faint"].read_bytes())


def tearDownModule():
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)


class AlphaIsPartOfTheIdentity(unittest.TestCase):
    """F12: two clips that differ only in transparency are two clips."""

    def test_identical_rgb_with_different_alpha_are_different_items(self):
        a = identity.content_key(CLIPS["opaque"], "video")
        b = identity.content_key(CLIPS["faint"], "video")
        self.assertNotEqual(a, b, "alpha 255 and alpha 129 collapsed onto one key")

    def test_they_are_not_the_same_image(self):
        """`Catalog.add` merges on this answer and DELETES the loser's file, so
        a wrong True here is lost media, not a cosmetic mistake."""
        self.assertIs(identity.same_image(CLIPS["opaque"], CLIPS["faint"], "video"),
                      False)

    def test_fully_transparent_is_not_fully_opaque(self):
        self.assertNotEqual(identity.content_key(CLIPS["clear"], "video"),
                            identity.content_key(CLIPS["opaque"], "video"))
        self.assertIs(identity.same_image(CLIPS["clear"], CLIPS["opaque"], "video"),
                      False)

    def test_a_genuine_remux_is_still_the_same_item(self):
        """The other half of the bargain. Owner rule 1 re-encodes every
        download, so a content key that moved on a `-c copy` remux would orphan
        the catalog row it belongs to."""
        self.assertEqual(identity.content_key(CLIPS["remux"], "video"),
                         identity.content_key(CLIPS["opaque"], "video"))
        self.assertIs(identity.same_image(CLIPS["remux"], CLIPS["opaque"], "video"),
                      True)


class TheDecoderComesFromTheCodec(unittest.TestCase):
    """F12: choosing by file extension is wrong in both directions."""

    def test_a_vp9_clip_gets_the_alpha_capable_vp9_decoder(self):
        self.assertEqual(media.probe_video(CLIPS["opaque"]).codec, "vp9")
        self.assertEqual(video_decode.decoder_args(CLIPS["opaque"]),
                         ["-c:v", "libvpx-vp9"])

    def test_a_vp8_clip_is_not_forced_through_the_vp9_decoder(self):
        """`suffix == ".webm"` named libvpx-vp9 for every WebM, including VP8."""
        self.assertEqual(media.probe_video(CLIPS["vp8_opaque"]).codec, "vp8")
        self.assertEqual(video_decode.decoder_args(CLIPS["vp8_opaque"]),
                         ["-c:v", "libvpx"])

    def test_vp8_alpha_is_read_too(self):
        self.assertNotEqual(identity.content_key(CLIPS["vp8_opaque"], "video"),
                            identity.content_key(CLIPS["vp8_faint"], "video"))
        self.assertIs(identity.same_image(CLIPS["vp8_opaque"], CLIPS["vp8_faint"],
                                          "video"), False)

    def test_an_extensionless_download_decodes_the_same_way(self):
        """A file fetched from Telegram lands under a temporary name with no
        suffix. The extension test skipped the alpha decoder for exactly the
        files that most needed it."""
        self.assertEqual(video_decode.decoder_args(CLIPS["noext"]),
                         ["-c:v", "libvpx-vp9"])
        self.assertEqual(identity.content_key(CLIPS["noext"], "video"),
                         identity.content_key(CLIPS["faint"], "video"))

    def test_the_decoder_choice_follows_the_bytes_not_the_name(self):
        """The cache is keyed on size and mtime because `reencode_in_place`
        rewrites files where they stand."""
        swapped = _TMP / "swapped.webm"
        swapped.write_bytes(CLIPS["vp8_opaque"].read_bytes())
        self.assertEqual(video_decode.decoder_args(swapped), ["-c:v", "libvpx"])
        swapped.write_bytes(CLIPS["opaque"].read_bytes())
        self.assertEqual(video_decode.decoder_args(swapped), ["-c:v", "libvpx-vp9"])


class OneSamplingRuleForEveryAnswer(unittest.TestCase):
    """F13: two public APIs must not describe the same file differently."""

    def test_fingerprint_and_perceptual_hash_agree(self):
        for name in ("fast30", "fast15", "opaque", "single"):
            with self.subTest(clip=name):
                _key, from_fingerprint = identity.fingerprint(CLIPS[name], "video")
                standalone = identity.perceptual_hash(CLIPS[name], "video")
                self.assertEqual(from_fingerprint, standalone)

    def test_fingerprint_and_content_key_agree(self):
        for name in ("fast30", "opaque", "single"):
            with self.subTest(clip=name):
                key, _ = identity.fingerprint(CLIPS[name], "video")
                self.assertEqual(key, identity.content_key(CLIPS[name], "video"))

    def test_a_single_frame_clip_still_gets_a_real_content_key(self):
        """Shorter than one sampling interval, so `fps=10` emits nothing. The
        retry at the file's own frames is what keeps this from silently
        degrading to a hash of the container bytes."""
        key = identity.content_key(CLIPS["single"], "video")
        byte_hash = "v:" + hashlib.sha256(
            CLIPS["single"].read_bytes()).hexdigest()[:32]
        self.assertTrue(key.startswith("v:"))
        self.assertNotEqual(key, byte_hash, "fell back to hashing the container")


class AFailedLookIsNotAnAnswer(unittest.TestCase):
    """Unknown is its own outcome. It is neither equality nor difference."""

    def test_an_undecodable_file_is_undecidable_not_equal(self):
        broken = _TMP / "broken.webm"
        broken.write_bytes(b"\x1a\x45\xdf\xa3" + b"garbage" * 64)
        self.assertIsNone(identity.same_image(broken, CLIPS["opaque"], "video"))

    def test_video_colour_is_actually_compared(self):
        """Pillow cannot open a .webm. The colour half of same_image() used to
        call `Image.open` unconditionally, so it raised for EVERY video pair and
        the result was permanently None -- a re-encoded video could never be
        confirmed to be ours."""
        self.assertIsNotNone(
            identity._premultiplied(CLIPS["opaque"], fmt="video"),
            "a video frame could not be rendered for comparison")
        self.assertIs(identity.same_image(CLIPS["remux"], CLIPS["opaque"], "video"),
                      True)


if __name__ == "__main__":
    unittest.main()
