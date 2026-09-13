"""R02: a video is a timeline, and one frame of it is not the picture.

`same_image(..., "video")` read frame zero and nothing else -- both halves of
its two-agreement rule (`perceptual_hash` and the premultiplied colour check)
went through `_first_video_frame`. Two real one-second 30 fps clips sharing ten
opening red frames, one then blue and the other green, had different content
keys and still compared equal. Reconciliation then bound the foreign clip's
file_unique_id and custom_emoji_id to our catalog item, and that survived
reopening SQLite.

Two things had to change, and the second only showed up under test:

* the comparison walks the whole stream, every frame pair under the same rule a
  static image gets, with no averaging across frames -- an average is exactly
  what lets one differing segment hide;
* for video it no longer accepts content-key equality as proof. The key hashes
  the 10 fps IDENTITY stream and a change shorter than one sampling interval
  falls between its frames: two clips differing in exactly ONE frame (flat blue
  against flat green, premultiplied mean delta 100.5) hash to the same key, and
  the old shortcut returned True from that equality before comparing anything.

Real encodes throughout. The question is what a decoder does with a real
timeline, and a synthetic byte string cannot ask it.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tests._media_fixtures import HAS_FFMPEG

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import collection_reconcile as cr  # noqa: E402
from emojikit import identity, media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

RED = (220, 20, 20, 255)
BLUE = (20, 20, 220, 255)
GREEN = (20, 220, 20, 255)


def _encode(tmp: Path, name: str, colours: list[tuple[int, int, int, int]]) -> Path:
    """One real clip, one PNG per frame, 30 fps."""
    d = tmp / name
    d.mkdir()
    for i, c in enumerate(colours):
        Image.new("RGBA", (100, 100), c).save(d / f"{i:04d}.png")
    out = tmp / f"{name}.webm"
    media._run([media.ffmpeg_path(), "-y", "-framerate", "30",
                "-i", str(d / "%04d.png"), "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(out)],
               capture=True)
    return out


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe")
class TheWholeTimelineIsCompared(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="r02-")
        cls.tmp = Path(cls._tmp.name)
        head = [RED] * 10
        cls.blue = _encode(cls.tmp, "blue", head + [BLUE] * 20)
        cls.green = _encode(cls.tmp, "green", head + [GREEN] * 20)
        cls.blue_again = _encode(cls.tmp, "blue_again", head + [BLUE] * 20)
        cls.short = _encode(cls.tmp, "short", head + [BLUE] * 5)
        spiked = head + [BLUE] * 20
        spiked[15] = GREEN
        cls.spiked = _encode(cls.tmp, "spiked", spiked)
        cls.fade = _encode(cls.tmp, "fade",
                           [(20, 20, 220, a) for a in range(0, 255, 8)][:30])
        cls.solid = _encode(cls.tmp, "solid", [BLUE] * 30)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def same(self, a: Path, b: Path):
        return identity.same_image(a, b, "video")

    def test_a_re_encode_of_the_same_clip_is_still_the_same_clip(self):
        """The positive control. Without it the rest proves only strictness."""
        self.assertIs(self.same(self.blue, self.blue_again), True)

    def test_the_same_opening_with_a_different_tail_is_not(self):
        self.assertIs(self.same(self.blue, self.green), False)

    def test_one_differing_frame_in_the_middle_is_not(self):
        """The case that also needed the content-key shortcut removed: these
        two hash to the SAME key at the identity sampling rate."""
        self.assertEqual(identity.content_key(self.blue, "video"),
                         identity.content_key(self.spiked, "video"),
                         "fixture no longer exercises the key-equality path")
        self.assertIs(self.same(self.blue, self.spiked), False)

    def test_a_shorter_clip_is_not_the_same_clip(self):
        self.assertIs(self.same(self.blue, self.short), False)

    def test_a_fade_in_is_not_the_solid_version_of_itself(self):
        """Alpha across the timeline, not just in frame zero."""
        self.assertIs(self.same(self.fade, self.solid), False)

    def test_an_unreadable_clip_is_undecidable_not_negative(self):
        broken = self.tmp / "broken.webm"
        broken.write_bytes(b"\x1a\x45\xdf\xa3not a real stream")
        self.assertIsNone(self.same(self.blue, broken))

    def test_the_comparison_is_bounded(self):
        """A ceiling that does not depend on the clip's length."""
        self.assertLessEqual(len(identity._frame_images(self.blue)),
                             identity.VIDEO_MAX_FRAMES)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe")
class ReconciliationWritesNothingForAForeignClip(unittest.TestCase):
    """The consequence, at the layer that persists it.

    `_resolve_sticker_key` is the shared contract every caller uses -- upload
    retries, fresh confirmation and catalog recovery all route through it -- so
    the assertion is made there rather than at one call site. Then SQLite is
    reopened, because the defect's cost was a row that survived the process.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="r02-rec-")
        cls.tmp = Path(cls._tmp.name)
        head = [RED] * 10
        cls.ours = _encode(cls.tmp, "ours", head + [BLUE] * 20)
        cls.foreign = _encode(cls.tmp, "foreign", head + [GREEN] * 20)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.db = self.tmp / f"{self.id().rsplit('.', 1)[-1]}.db"
        self.db.unlink(missing_ok=True)
        self.cat = Catalog(self.db)
        self.addCleanup(self.cat.close)
        self.cat.add(content_key=identity.content_key(self.ours, "video"),
                     fmt="video", file_path=self.ours,
                     phash=identity.perceptual_hash(self.ours, "video"))

    def resolve(self, served: Path):
        class _Stub:
            def download_file(self, file_id, dest):
                Path(dest).write_bytes(served.read_bytes())

        # is_video matters: the resolver takes the format from the STICKER, and
        # without it a .webm is opened as a still and every case errors alike.
        sticker = {"file_unique_id": "fuid-" + served.stem, "file_id": "fid",
                   "is_video": True, "is_animated": False}
        return cr._resolve_sticker_key(_Stub(), self.cat, sticker, self.tmp / "dl")

    def test_a_foreign_clip_sharing_our_opening_resolves_to_nothing(self):
        self.assertIsNone(self.resolve(self.foreign))

    def test_our_own_clip_still_resolves_to_our_key(self):
        self.assertEqual(self.resolve(self.ours),
                         identity.content_key(self.ours, "video"))

    def test_no_foreign_fuid_is_bound_and_it_survives_a_reopen(self):
        try:
            self.resolve(self.foreign)
        except cr.Unresolvable:
            pass
        self.cat.close()
        con = sqlite3.connect(self.db)
        try:
            bound = [r[0] for r in con.execute(
                "SELECT file_unique_id FROM seen_files")]
        finally:
            con.close()
        self.assertNotIn("fuid-foreign", bound,
                         "a foreign clip was bound to our catalog item")


if __name__ == "__main__":
    unittest.main()
