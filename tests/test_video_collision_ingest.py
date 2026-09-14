"""A01/A02: native-frame differences survive storage, dedup and recovery.

Small real VP9 fixtures are encoded once; each scenario gets a disposable
catalog. Only Telegram transport and log initialization are replaced.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import add_media
from emojikit import collection_reconcile as cr
import fetch_emoji_ids
import fetch_pack
from emojikit import identity, media, video_decode
from emojikit.catalog import Catalog
from emojikit.ingest import store_media


RED = bytes((220, 20, 20, 255)) * (100 * 100)
BLUE = bytes((20, 20, 220, 255)) * (100 * 100)
TEST_ROOT = Path(__file__).resolve().parents[1] / "logs"


def encode(directory: Path, name: str, frames: list[bytes], *, pts: str = "") -> Path:
    source = directory / (name + ".rgba")
    source.write_bytes(b"".join(frames))
    out = directory / (name + ".webm")
    timing = ["-vf", "settb=1/1000,setpts=" + pts,
              "-fps_mode", "passthrough", "-enc_time_base", "1/1000"] if pts else []
    media._run([media.ffmpeg_path(), "-y", "-v", "error", "-f", "rawvideo",
                "-pixel_format", "rgba", "-video_size", "100x100", "-framerate", "30",
                "-i", str(source), *timing, "-c:v", "libvpx-vp9", "-threads", "1",
                "-cpu-used", "8", "-lossless", "1", "-pix_fmt", "yuva420p",
                "-auto-alt-ref", "0", str(out)], capture=True, timeout=30)
    source.unlink()
    return out


class FakeTelegram:
    def __init__(self, clips: dict[str, Path]):
        self.clips = clips

    @staticmethod
    def sticker(name):
        return {"file_id": name, "file_unique_id": "fuid-" + name,
                "custom_emoji_id": name, "is_video": True, "emoji": "x"}

    def get_sticker_set(self, name):
        return {"name": name, "stickers": [self.sticker(k) for k in self.clips]}

    def get_custom_emoji_stickers(self, ids):
        return [self.sticker(k) for k in ids]

    def download_file(self, file_id, dest):
        shutil.copyfile(self.clips[file_id], dest)


class NativeVideoBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise AssertionError("ffmpeg and ffprobe are required for A01/A02 coverage")
        TEST_ROOT.mkdir(exist_ok=True)
        cls.fixture = tempfile.TemporaryDirectory(prefix="a01-codecs-", dir=TEST_ROOT)
        cls.addClassCleanup(cls.fixture.cleanup)
        root = Path(cls.fixture.name)
        cls.clips = {"red": encode(root, "red", [RED] * 30),
                     "short": encode(root, "short", [RED] * 29)}
        for name, index in (("first", 0), ("middle", 15), ("last", 29)):
            frames = [RED] * 30
            frames[index] = BLUE
            cls.clips[name] = encode(root, name, frames)
        cls.clips["vfr"] = encode(root, "vfr", [RED, BLUE, RED, RED],
                                  pts="if(eq(N\\,0)\\,0\\,if(eq(N\\,1)\\,490\\,"
                                      "if(eq(N\\,2)\\,500\\,967)))")
        cls.clips["remux"] = root / "remux.webm"
        media._run([media.ffmpeg_path(), "-y", "-v", "error", "-i",
                    str(cls.clips["middle"]), "-c", "copy", str(cls.clips["remux"])],
                   capture=True, timeout=30)
        pattern = b"".join(bytes((x * 2, y * 2, (x + y) % 256, 255))
                           for y in range(100) for x in range(100))
        cls.clips["pattern"] = encode(root, "pattern", [pattern] * 6)
        cls.clips["encoded"] = root / "encoded.webm"
        media._run([media.ffmpeg_path(), "-y", "-v", "error", "-c:v", "libvpx-vp9",
                    "-i", str(cls.clips["pattern"]), "-c:v", "libvpx-vp9", "-threads", "1",
                    "-crf", "35", "-b:v", "0", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
                    str(cls.clips["encoded"])], capture=True, timeout=30)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="a01-state-", dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def copy(self, name):
        out = self.root / (name + ".webm")
        shutil.copyfile(self.clips[name], out)
        return out

    def add(self, cat, name):
        path = self.copy(name)
        key, phash = identity.fingerprint(path, "video")
        return cat.add(content_key=key, fmt="video", file_path=path, phash=phash,
                       file_unique_id="fuid-" + name)

    def test_collision_preserves_both_files_and_distinct_identifiers_after_reopen(self):
        self.assertEqual(identity.content_key(self.clips["red"], "video"),
                         identity.content_key(self.clips["middle"], "video"))
        db = self.root / "catalog.db"
        with Catalog(db) as cat:
            red, _ = self.add(cat, "red")
            middle, is_new = self.add(cat, "middle")
            self.assertTrue(is_new, "a sampled-key collision discarded a different clip")
            self.assertNotEqual(red, middle)
            self.assertTrue((self.root / "middle.webm").is_file())
            alias, duplicate = self.add(cat, "remux")
            self.assertFalse(duplicate)
            self.assertEqual(alias, middle)
        with Catalog(db) as cat:
            self.assertEqual(len(cat.all_items()), 2)
            self.assertEqual(cat.seen_file_unique_id("fuid-red"), red)
            self.assertEqual(cat.seen_file_unique_id("fuid-middle"), middle)
            self.assertEqual(cat.seen_file_unique_id("fuid-remux"), middle)
            self.assertIs(identity.same_image(Path(cat.get(red).file_path),
                                              Path(cat.get(middle).file_path), "video"), False)

    def test_different_unmatched_tail_is_checked_in_both_directions(self):
        for a, b in (("short", "last"), ("last", "short")):
            with self.subTest(a=a, b=b):
                self.assertIs(identity.same_image(self.clips[a], self.clips[b], "video"), False)
        self.assertIs(identity.same_image(self.clips["short"], self.clips["red"], "video"), True)
        self.assertIs(identity.same_image(self.clips["red"], self.clips["short"], "video"), True)

    def test_native_first_last_and_variable_rate_frames_cannot_disappear(self):
        for name in ("first", "middle", "last", "vfr"):
            with self.subTest(name=name):
                self.assertIs(identity.same_image(self.clips["red"], self.clips[name], "video"), False)

    def test_exact_lookup_recovery_never_binds_the_foreign_clip(self):
        with Catalog(self.root / "catalog.db") as cat:
            self.add(cat, "red")
            tg = FakeTelegram({"foreign": self.clips["middle"]})
            self.assertIsNone(cr._resolve_sticker_key(tg, cat, tg.sticker("foreign"), self.root / "tmp"))
            self.assertIsNone(cat.seen_file_unique_id("fuid-foreign"))
            self.assertEqual(cat.publication_bases(), [])

    def test_recovery_and_resume_bind_the_colliding_item_only(self):
        db = self.root / "catalog.db"
        with Catalog(db) as cat:
            red, _ = self.add(cat, "red")
            middle, _ = self.add(cat, "middle")
            # A stale derived hash must not exclude a sampled-key candidate.
            cat.db.execute("UPDATE items SET phash=-1 WHERE content_key=?", (middle,))
            cat.db.commit()
            tg = FakeTelegram({"live": self.clips["remux"]})
            state = {"name": "fixture", "fmt": "video", "keys": []}
            self.assertEqual(cr.reconcile_set(tg, cat, state, self.root, "audit"), 1)
            self.assertEqual(state["keys"], [middle])
        with Catalog(db) as cat:
            self.assertEqual(cat.seen_file_unique_id("fuid-live"), middle)
            self.assertEqual(cat.custom_emoji_id_for("audit", middle), "live")
            self.assertFalse(cat.is_published("audit", red))
            self.assertEqual(cr.reconcile_set(tg, cat, state, self.root, "audit"), 1)
            self.assertEqual(state["keys"], [middle])

    def test_upload_confirmation_checks_the_unmatched_terminal_frame(self):
        with Catalog(self.root / "catalog.db") as cat:
            short, _ = self.add(cat, "short")
            tg = FakeTelegram({"live": self.clips["last"]})
            with self.assertRaises(cr.SetDrift):
                cr._confirm_new_upload(tg, cat, "fixture", short, {}, self.root / "tmp")
            self.assertIsNone(cat.seen_file_unique_id("fuid-live"))
            self.assertEqual(cat.publication_bases(), [])

    def test_near_merge_verifies_content_and_accepts_a_real_reencode(self):
        with Catalog(self.root / "catalog.db", phash_threshold=4) as cat:
            red, _ = self.add(cat, "red")
            last, different = self.add(cat, "last")
            self.assertTrue(different, "a similar opening frame is not content equivalence")
            self.assertNotEqual(red, last)
            pattern, _ = self.add(cat, "pattern")
            self.assertNotEqual(identity.content_key(self.clips["pattern"], "video"),
                                identity.content_key(self.clips["encoded"], "video"))
            encoded, new = self.add(cat, "encoded")
            self.assertFalse(new)
            self.assertEqual(encoded, pattern)

    def test_unreadable_exact_candidate_preserves_input_and_binds_nothing(self):
        with Catalog(self.root / "catalog.db") as cat:
            red, _ = self.add(cat, "red")
            Path(cat.get(red).file_path).unlink()
            path = self.copy("middle")
            with self.assertRaises(media.MediaError):
                cat.add(content_key=red, fmt="video", file_path=path, file_unique_id="foreign")
            self.assertTrue(path.is_file())
            self.assertIsNone(cat.seen_file_unique_id("foreign"))

    def test_untracked_storage_collision_never_overwrites_the_existing_bytes(self):
        source = self.copy("middle")
        destination = self.root / "occupied.webm"
        destination.write_bytes(b"unrelated sentinel")
        result = store_media(source, destination, "video", identity.content_key(source, "video"))
        self.assertEqual(destination.read_bytes(), b"unrelated sentinel")
        self.assertNotEqual(result, destination)
        self.assertEqual(result.read_bytes(), self.clips["middle"].read_bytes())

    def test_all_ingest_paths_preserve_existing_destination_and_incoming_difference(self):
        for route in ("pack", "ids", "local"):
            with self.subTest(route=route):
                data = self.root / route
                tmp = data / "tmp"
                tmp.mkdir(parents=True)
                sentinel = tmp / "another-writer.download"
                sentinel.write_bytes(b"not this run's scratch file")
                tg = FakeTelegram({"red": self.clips["red"], "middle": self.clips["middle"]})
                if route == "local":
                    with mock.patch.object(add_media, "setup_logging"), contextlib.redirect_stdout(io.StringIO()):
                        result = add_media.main([str(p) for p in tg.clips.values()] + ["--data-dir", str(data)])
                    self.assertEqual(result, 0)
                    self.assertEqual(sentinel.read_bytes(), b"not this run's scratch file")
                else:
                    with Catalog(data / "catalog.db") as cat:
                        if route == "pack":
                            counts = fetch_pack.fetch_one(tg, cat, "fixture", data, tmp, repaintable="keep")
                        else:
                            counts = fetch_emoji_ids.fetch_ids(tg, cat, list(tg.clips), data, tmp, repaintable="keep")
                        self.assertEqual(counts["failed"], 0)
                with Catalog(data / "catalog.db") as cat:
                    items = cat.all_items()
                    self.assertEqual(len(items), 2, "storage-before-catalog discarded the collision")
                    self.assertNotEqual(items[0].file_path, items[1].file_path)
                    self.assertTrue(all(Path(it.file_path).is_file() for it in items))
                    self.assertIs(identity.same_image(Path(items[0].file_path), Path(items[1].file_path), "video"), False)


class TimelineAlignment(unittest.TestCase):
    def test_equal_frames_at_different_times_do_not_prove_equivalent_timelines(self):
        raw = (RED[:video_decode.FRAME_BYTES] + BLUE[:video_decode.FRAME_BYTES])

        def timeline(path):
            return raw, (0.0, 0.3 if path.name == "a" else 0.4), 1.0

        with mock.patch.object(video_decode, "timeline_rgba", side_effect=timeline):
            self.assertIs(identity.same_image(Path("a"), Path("b"), "video"), False)


if __name__ == "__main__":
    unittest.main()
