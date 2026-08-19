"""Bounds around the media/ingest layer.

Three invariants that used to be missing:

* every ffmpeg/ffprobe child runs under a finite timeout (H-13);
* the near-duplicate Hamming threshold stays inside the 64 bits a dHash has,
  so it can never merge the whole catalog into one emoji (H-10);
* blank media is refused at conversion, so an invisible emoji never reaches the
  catalog or a pack slot (M-15).

The timeout tests replace ffmpeg/ffprobe with a stand-in that never exits, so
they prove the bound without waiting for a real decoder to wedge.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Allow running from the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import add_media  # noqa: E402
from build_pack import EXIT_FAILED, EXIT_USAGE  # noqa: E402
from emojikit import media  # noqa: E402
from emojikit.catalog import (Catalog, PHASH_BITS,  # noqa: E402
                              PHASH_MAX_THRESHOLD, check_phash_threshold)

# The stand-in binaries block forever; this timeout is the only thing that can
# stop them. Kept at the 1 s floor so the suite spends ~1 s per call site.
FF_TIMEOUT = 1
# Generous ceiling: what matters is that the call returns at all. Before the
# fix it never did, and the whole run hung.
MAX_ELAPSED = 60

_HANG = "import time\nwhile True:\n    time.sleep(60)\n"


def _install_hanging_ff(directory: Path) -> None:
    """Create ffmpeg/ffprobe stand-ins in ``directory`` that never exit."""
    script = directory / "hang.py"
    script.write_text(_HANG, encoding="utf-8")
    for name in ("ffmpeg", "ffprobe"):
        if os.name == "nt":
            # CreateProcess runs a .bat through cmd.exe, so the process that
            # actually hangs is a GRANDchild -- exactly what the tree kill must
            # reach. Arguments are dropped: the fake ignores them anyway.
            launcher = directory / f"{name}.bat"
            launcher.write_text(f'@"{sys.executable}" "{script}"\r\n',
                                encoding="utf-8")
        else:
            launcher = directory / name
            launcher.write_text(f"#!{sys.executable}\n{_HANG}", encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


class TestChildProcessTimeout(unittest.TestCase):
    """A hung ffmpeg/ffprobe must surface as MediaError, not as a stalled run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _install_hanging_ff(self.tmp)
        env = mock.patch.dict(os.environ, {
            "PATH": f"{self.tmp}{os.pathsep}{os.environ.get('PATH', '')}",
            "EMOJI_FFMPEG_TIMEOUT": str(FF_TIMEOUT),
        })
        env.start()
        self.addCleanup(env.stop)
        self.sample = self.tmp / "clip.webm"
        self.sample.write_bytes(b"\x1a\x45\xdf\xa3 not a real video")

    def _assert_bounded(self, call) -> None:
        started = time.monotonic()
        with self.assertRaises(media.MediaError):
            call()
        self.assertLess(time.monotonic() - started, MAX_ELAPSED)

    def test_every_ffmpeg_call_site_is_bounded(self):
        for name, call in (
            ("probe_video", lambda: media.probe_video(self.sample)),
            ("to_video_webm",
             lambda: media.to_video_webm(self.sample, self.tmp / "out.webm")),
            ("_video_content_digest",
             lambda: media._video_content_digest(self.sample)),
            # fingerprint() shares that decode now. It used to catch the
            # failure and return a byte-hash key instead -- a key that looks
            # valid, never dedups, and hides the fact that ffmpeg hung.
            ("fingerprint",
             lambda: media.fingerprint(self.sample, "video")),
            ("_video_frames_rgba",
             lambda: media._video_frames_rgba(self.sample)),
            ("_first_video_frame",
             lambda: media._first_video_frame(self.sample)),
        ):
            with self.subTest(call=name):
                self._assert_bounded(call)

    def test_timeout_is_configurable(self):
        self.assertEqual(media.ff_timeout(), FF_TIMEOUT)
        with mock.patch.dict(os.environ, {"EMOJI_FFMPEG_TIMEOUT": "nonsense"}):
            self.assertEqual(media.ff_timeout(), media.FFMPEG_TIMEOUT)

    def test_explicit_timeout_kills_the_child(self):
        started = time.monotonic()
        with self.assertRaises(media.MediaError):
            media._run([media.ffmpeg_path(), "-version"], capture=True,
                       timeout=0.5)
        self.assertLess(time.monotonic() - started, MAX_ELAPSED)


def _pseudo_noise(seed: int) -> Image.Image:
    """A deterministic, version-independent image unrelated to any other seed.

    Built arithmetically rather than with ``random`` so the Hamming distances
    asserted below are the same on every machine and Python build.
    """
    px = bytes(((i * 2654435761 + seed * 40503) >> 7) & 0xFF for i in range(64 * 64))
    return Image.frombytes("L", (64, 64), px).convert("RGBA")


class TestPhashThresholdRange(unittest.TestCase):
    """A dHash is 64 bits: a big threshold merges unrelated same-format items."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _catalog(self, threshold: int) -> Catalog:
        return Catalog(self.tmp / "catalog.db", phash_threshold=threshold)

    def _file(self, name: str) -> Path:
        p = self.tmp / name
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(p)
        return p

    def test_usable_thresholds_are_accepted(self):
        for value in (-1, 0, 8, PHASH_MAX_THRESHOLD):
            with self.subTest(threshold=value):
                with self._catalog(value) as cat:
                    self.assertEqual(cat.phash_threshold, value)

    def test_out_of_range_thresholds_are_refused(self):
        # PHASH_BITS itself is the worst case, not the widest usable setting:
        # `hamming(a, b) <= 64` is true for EVERY pair of 64-bit hashes.
        for value in (-2, PHASH_MAX_THRESHOLD + 1, PHASH_BITS, 999):
            with self.subTest(threshold=value):
                with self.assertRaises(ValueError):
                    self._catalog(value)

    def test_unrelated_images_survive_the_widest_allowed_threshold(self):
        """The bound is what stops a whole catalog collapsing into one emoji."""
        a, b = _pseudo_noise(1), _pseudo_noise(2)
        distance = media.hamming(media._dhash(a), media._dhash(b))
        # Two unrelated pictures land near half the hash. The old ceiling (64)
        # is >= any distance at all, so it merged them; the new one cannot.
        self.assertGreater(distance, PHASH_MAX_THRESHOLD)
        self.assertLessEqual(distance, PHASH_BITS)

        with self._catalog(PHASH_MAX_THRESHOLD) as cat:
            cat.add(content_key="k-a", fmt="static", file_path=self._file("a.png"),
                    phash=media._dhash(a))
            _, is_new = cat.add(content_key="k-b", fmt="static",
                                file_path=self._file("b.png"),
                                phash=media._dhash(b))
            self.assertTrue(is_new, "unrelated image merged into the first one")
            self.assertEqual(len(cat.all_items()), 2)

    def test_cli_rejects_an_out_of_range_threshold(self):
        stderr = io.StringIO()
        with mock.patch.object(add_media, "setup_logging", lambda *a, **k: None), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as ctx:
            add_media.main(["--phash-threshold", "999", "--data-dir",
                            str(self.tmp)])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertIn("out of range", stderr.getvalue())

    def test_check_phash_threshold_refuses_the_full_hash_width(self):
        # The exact value the old bound allowed, and the one that collapses
        # every same-format item onto a single catalog row.
        with self.assertRaises(ValueError):
            check_phash_threshold(PHASH_BITS)


class TestBlankMediaRefused(unittest.TestCase):
    """A transparent emoji is invisible forever -- never ingest one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "collection"

    def _png(self, name: str, color) -> Path:
        p = self.tmp / name
        Image.new("RGBA", (120, 90), color).save(p)
        return p

    def test_fully_transparent_source_is_refused(self):
        out = self.tmp / "out.png"
        with self.assertRaises(media.MediaError):
            media.to_static_png(self._png("blank.png", (0, 0, 0, 0)), out)
        self.assertFalse(out.exists())

    def test_barely_visible_source_is_refused(self):
        # Alpha at or below VISIBLE_ALPHA renders as nothing, even though the
        # channel is not literally empty (so getbbox() alone would pass it).
        out = self.tmp / "faint.png"
        with self.assertRaises(media.MediaError):
            media.to_static_png(
                self._png("faint.png", (255, 0, 0, media.VISIBLE_ALPHA)), out)

    def test_visible_source_still_converts(self):
        out = media.to_static_png(self._png("solid.png", (255, 0, 0, 255)),
                                  self.tmp / "solid_out.png")
        with Image.open(out) as im:
            self.assertEqual(im.size, (media.SIZE, media.SIZE))

    def test_add_media_does_not_ingest_a_blank_image(self):
        blank = self._png("blank.png", (0, 0, 0, 0))
        with mock.patch.object(add_media, "setup_logging", lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = add_media.main([str(blank), "--data-dir", str(self.data)])
        self.assertEqual(rc, EXIT_FAILED)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.all_items(), [])


if __name__ == "__main__":
    unittest.main()
