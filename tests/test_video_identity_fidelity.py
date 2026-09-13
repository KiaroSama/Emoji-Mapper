"""R01: an identity-grade decode either carries the alpha or says it could not.

The previous round taught this module to name `libvpx-vp9` so a VP9 clip's
separate alpha layer survives a decode. It did not teach it what to do when that
cannot be arranged, and every one of those paths degraded to a plain decode:

* a missing `libvpx-vp9` answered `[]`,
* a failed codec probe answered `[]` **and cached it**, so one transient ffprobe
  error made every later decode of that file in the process lose its alpha,
* a container that named no codec answered `[]`.

A plain decode of a VP9 clip drops its transparency, so two clips differing only
in opacity produce ONE content key -- and `Catalog.add` merges on an equal key,
then `_drop_unreferenced` deletes the file it merged away. The defect's cost is
measured in deleted media, which is why the answer here is an exception rather
than a warning: "a warning followed by native decoding is not a fix".

Real VP9 and VP8 encodes, because the whole question is what a decoder does with
a real alpha layer; a synthetic byte string would prove nothing about it.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._media_fixtures import HAS_FFMPEG

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import identity, media, video_decode  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402
from emojikit.errors import UndecodableVideo  # noqa: E402


def _encode(tmp: Path, name: str, alpha: int, codec: str = "libvpx-vp9") -> Path:
    """A real clip whose ONLY difference from its sibling is transparency."""
    src = tmp / f"{name}.png"
    Image.new("RGBA", (100, 100), (200, 30, 30, alpha)).save(src)
    out = tmp / f"{name}.webm"
    media._run([media.ffmpeg_path(), "-y", "-loop", "1", "-t", "1", "-i", str(src),
                "-c:v", codec, "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
                "-r", "10", str(out)], capture=True)
    return out


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe")
class AlphaFidelityIsEstablishedOrRefused(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="r01-")
        cls.tmp = Path(cls._tmp.name)
        cls.opaque = _encode(cls.tmp, "opaque", 255)
        cls.translucent = _encode(cls.tmp, "translucent", 128)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # Every case starts from a cold cache: the defect under test IS a cache
        # entry, so a warm one would hide it.
        video_decode._decoder_cache.clear()
        video_decode._frame_cache.clear()
        video_decode._available.clear()
        self.addCleanup(video_decode._decoder_cache.clear)
        self.addCleanup(video_decode._frame_cache.clear)
        self.addCleanup(video_decode._available.clear)

    def test_a_healthy_decoder_still_separates_them(self):
        """The positive control. Without this the rest proves only strictness."""
        a = identity.content_key(self.opaque, "video")
        b = identity.content_key(self.translucent, "video")
        self.assertNotEqual(a, b, "alpha is part of a video's identity")

    def test_a_missing_decoder_refuses_instead_of_dropping_alpha(self):
        with mock.patch.object(video_decode, "decoder_available", lambda n: False):
            with self.assertRaises(UndecodableVideo):
                identity.content_key(self.opaque, "video")

    def test_a_missing_decoder_cannot_collapse_two_clips_into_one_key(self):
        """Stated as the outcome that cost media, not as the mechanism."""
        keys = []
        with mock.patch.object(video_decode, "decoder_available", lambda n: False):
            for clip in (self.opaque, self.translucent):
                try:
                    keys.append(identity.content_key(clip, "video"))
                except UndecodableVideo:
                    keys.append(None)
        self.assertEqual(keys, [None, None])

    def test_a_probe_failure_is_not_cached_as_a_successful_fallback(self):
        """One transient ffprobe error used to be permanent for that file."""
        calls = {"n": 0}
        real = media.probe_video

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ffprobe: transient failure")
            return real(path)

        with mock.patch.object(media, "probe_video", flaky):
            with self.assertRaises(UndecodableVideo):
                video_decode.decoder_args(self.opaque)
            # Same process, same file, healthy probe: it must ask again.
            self.assertEqual(video_decode.decoder_args(self.opaque),
                             ["-c:v", "libvpx-vp9"])
        self.assertEqual(calls["n"], 2, "the second probe never ran")

    def test_a_container_naming_no_codec_is_not_read_as_needing_nothing(self):
        """Missing metadata is not evidence; it is the absence of evidence."""
        blank = media.VideoInfo(width=100, height=100, duration=1.0, codec="",
                                fps=10.0, container="webm", has_audio=False)
        with mock.patch.object(media, "probe_video", lambda p: blank):
            with self.assertRaises(UndecodableVideo):
                video_decode.decoder_args(self.opaque)

    def test_an_unreadable_capability_check_is_not_an_answer_of_no(self):
        def boom(*a, **kw):
            raise RuntimeError("ffmpeg did not run")

        with mock.patch.object(media, "_run", boom):
            with self.assertRaises(UndecodableVideo):
                video_decode.decoder_available("libvpx-vp9")
        self.assertEqual(video_decode._available, {}, "a non-answer was cached")

    def test_an_undecodable_video_never_reaches_the_catalog(self):
        """The whole point: no insertion, no merge, and no file deleted."""
        db = self.tmp / "r01.db"
        with mock.patch.object(video_decode, "decoder_available", lambda n: False):
            with self.assertRaises(UndecodableVideo):
                key = identity.content_key(self.opaque, "video")
                with Catalog(db) as cat:
                    cat.add(content_key=key, fmt="video", file_path=self.opaque)
        self.assertTrue(self.opaque.is_file())
        self.assertTrue(self.translucent.is_file())

    def test_the_three_public_apis_agree_after_a_recovery(self):
        """A refusal must not leave the module answering differently afterwards."""
        with mock.patch.object(video_decode, "decoder_available", lambda n: False):
            with self.assertRaises(UndecodableVideo):
                identity.fingerprint(self.opaque, "video")
        key, phash = identity.fingerprint(self.opaque, "video")
        self.assertEqual(key, identity.content_key(self.opaque, "video"))
        self.assertEqual(phash, identity.perceptual_hash(self.opaque, "video"))

    def test_concurrent_callers_get_one_consistent_answer(self):
        out: list[object] = []

        def ask():
            try:
                out.append(tuple(video_decode.decoder_args(self.opaque)))
            except Exception as exc:                      # noqa: BLE001
                out.append(type(exc).__name__)

        threads = [threading.Thread(target=ask) for _ in range(8)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        self.assertFalse([t for t in threads if t.is_alive()], "a caller hung")
        self.assertEqual(set(out), {("-c:v", "libvpx-vp9")})

    def test_a_still_image_is_not_subjected_to_the_video_rule(self):
        """Conversion of a PNG must not start failing because a probe is strict."""
        png = self.tmp / "still.png"
        Image.new("RGBA", (80, 60), (10, 200, 90, 255)).save(png)
        self.assertEqual(video_decode.decoder_args(png), [])


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe")
class Vp8IsHeldToTheSameRule(unittest.TestCase):
    """VP8 hides alpha the same way, and `libvpx` is its reader.

    Encoded with `-auto-alt-ref 0` because libvpx refuses transparency when it
    is allowed to emit alt-ref frames.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="r01-vp8-")
        cls.tmp = Path(cls._tmp.name)
        cls.opaque = _encode(cls.tmp, "vp8-opaque", 255, codec="libvpx")
        cls.translucent = _encode(cls.tmp, "vp8-translucent", 120, codec="libvpx")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        video_decode._decoder_cache.clear()
        video_decode._frame_cache.clear()
        video_decode._available.clear()

    def test_vp8_alpha_separates_two_clips(self):
        self.assertNotEqual(identity.content_key(self.opaque, "video"),
                            identity.content_key(self.translucent, "video"))

    def test_vp8_without_its_decoder_refuses(self):
        with mock.patch.object(video_decode, "decoder_available", lambda n: False):
            with self.assertRaises(UndecodableVideo):
                identity.content_key(self.opaque, "video")


if __name__ == "__main__":
    unittest.main()
