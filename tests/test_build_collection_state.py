"""Regression tests for build_collection's state, live-set and CLI contracts.

Every test here fails on the pre-fix behaviour:

* a plan/state file that EXISTS but cannot be parsed was swallowed into the
  empty default -- i.e. "nothing published yet" -- which re-uploads the pack;
* state written non-atomically could be truncated by a crash mid-write;
* "set is missing" and "live state unknown" both arrived as ``None`` and the
  stale recorded count was used as the live capacity;
* only stickers AFTER the recorded prefix were inspected, so a manual
  delete/reorder/replace inside the prefix was invisible and every
  custom_emoji_id written afterwards pointed at the wrong emoji;
* two publishers could run against one pack family at once;
* ``--formats garbage`` exited 0 having done nothing, ``--per-set 0`` divided
  by zero, and the dry-run set count ignored the brand logo's slot;
* a video was judged blank by its FIRST frame only, and that verdict is a
  permanent skip.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import build_collection as bc  # noqa: E402
from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE,  # noqa: E402
                        LiveStateUnknown, LockBusy, SetState, exclusive_lock)
from emojikit.catalog import Catalog  # noqa: E402

SET = "pks1_by_YourEmojiBot"


def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(20, 80):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


class FakeTG:
    """In-memory Telegram with the tri-state probe the real client exposes."""

    def __init__(self, sets=None, unknown=()):
        self.sets: dict[str, list[dict]] = dict(sets or {})
        self.unknown = set(unknown)          # names whose live state is unknown

    def probe_set_state(self, name):
        if name in self.unknown:
            return SetState.UNKNOWN, None
        if name in self.sets:
            return SetState.EXISTS, {"stickers": list(self.sets[name])}
        return SetState.MISSING, None

    def get_sticker_set(self, name):
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": list(self.sets[name])}


def _sticker(fuid: str, cid: str) -> dict:
    return {"file_unique_id": fuid, "custom_emoji_id": cid}


class _CatalogFixture(unittest.TestCase):
    """Two static items whose PUBLISHED copies are known as UP-item<i>."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.keys = []
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(2):
                p = self.data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, 60 * (i + 1), 200, 255))
                key = f"s:item{i:030d}"
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["\U0001F600"], keywords=[f"item{i}"])
                cat.record_file_unique_id(f"UP-item{i}", key)
                self.keys.append(key)

    def tearDown(self):
        self.tmp.cleanup()

    def _state_set(self, keys=None, live=2, logo=False) -> dict:
        return {"fmt": "static", "index": 1, "name": SET, "title": "Pack 1",
                "live": live, "logo": logo,
                "keys": list(self.keys if keys is None else keys)}


# --------------------------------------------------------------------------- #
# C-10 / H-01: state and plan files fail closed, and are written atomically
# --------------------------------------------------------------------------- #
class StateFileContract(_CatalogFixture):
    def test_absent_file_uses_the_default(self):
        self.assertEqual(bc.load_json(self.data / "nope.json", {"d": 1}), {"d": 1})

    def test_truncated_state_refuses_to_start_from_scratch(self):
        path = bc._state_path(self.data, "pk")
        path.write_text('{"base": "pk", "sets": [{"name": "pks1', encoding="utf-8")
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_truncated_plan_refuses_to_refreeze(self):
        bc._plan_path(self.data, "pk").write_text('{"static": ["s:a"',
                                                  encoding="utf-8")
        with self.assertRaises(bc.StateError):
            bc.load_plan(self.data, "pk")

    def test_plan_of_the_wrong_shape_is_rejected(self):
        bc._plan_path(self.data, "pk").write_text('{"static": "s:a"}',
                                                  encoding="utf-8")
        with self.assertRaises(bc.StateError):
            bc.load_plan(self.data, "pk")

    def test_state_of_another_pack_family_is_rejected(self):
        bc.save_json(bc._state_path(self.data, "pk"),
                     {"base": "other", "sets": [], "sent": []})
        with self.assertRaises(bc.StateError) as ctx:
            bc.load_state(self.data, "pk")
        self.assertIn("other", str(ctx.exception))

    def test_state_schema_is_checked(self):
        bc.save_json(bc._state_path(self.data, "pk"),
                     {"base": "pk", "sets": [{"name": SET, "fmt": "nope",
                                              "index": 1}], "sent": []})
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_a_failed_write_leaves_the_previous_state_intact(self):
        path = bc._state_path(self.data, "pk")
        bc.save_json(path, {"base": "pk", "sets": [], "sent": ["first"]})
        # Simulate the process dying at the very end of the write: with a
        # non-atomic write_text the destination is already truncated by then.
        with mock.patch("build_pack.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                bc.save_json(path, {"base": "pk", "sets": [], "sent": ["second"]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sent"],
                         ["first"])

    def test_main_stops_on_an_unreadable_plan(self):
        bc._plan_path(self.data, "pk").write_text("{oops", encoding="utf-8")
        self.assertEqual(_main("--base", "pk", "--title", "T",
                               "--data-dir", str(self.data), "--dry-run"),
                         EXIT_FAILED)


# --------------------------------------------------------------------------- #
# H-02 / H-03 / H-04: live-set identity
# --------------------------------------------------------------------------- #
class LiveSetDrift(_CatalogFixture):
    def _reconcile(self, tg, s):
        with Catalog(self.data / "catalog.db") as cat:
            return bc.reconcile_set(tg, cat, s, self.data, "pk")

    def test_unknown_live_state_aborts_instead_of_guessing(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                               _sticker("UP-item1", "c1")]}, unknown=[SET])
        s = self._state_set()
        with self.assertRaises(LiveStateUnknown):
            self._reconcile(tg, s)
        self.assertEqual(s["live"], 2)          # untouched: nothing was guessed

    def test_deleted_set_is_reported_not_treated_as_the_recorded_count(self):
        tg = FakeTG(sets={})                    # owner deleted the whole pack
        with self.assertRaises(bc.SetDrift) as ctx:
            self._reconcile(tg, self._state_set())
        self.assertIn("no longer exists", str(ctx.exception))

    def test_shrunk_set_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0")]})
        with self.assertRaises(bc.SetDrift):
            self._reconcile(tg, self._state_set())

    def test_reordered_set_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item1", "c1"),
                                _sticker("UP-item0", "c0")]})
        with self.assertRaises(bc.SetDrift) as ctx:
            self._reconcile(tg, self._state_set())
        self.assertIn("position 0", str(ctx.exception))

    def test_insert_inside_the_recorded_prefix_is_drift(self):
        # A hand-added sticker in the middle shifts our items one position on:
        # item1 now sits in the tail, where a tail-only reconcile would happily
        # attribute (and later re-publish) it a second time.
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("MANUAL", "cx"),
                                _sticker("UP-item1", "c1")]})
        s = self._state_set(keys=[self.keys[0]])
        with self.assertRaises(bc.SetDrift):
            self._reconcile(tg, s)
        self.assertEqual(s["keys"], [self.keys[0]])   # nothing mis-attributed

    def test_the_same_emoji_live_twice_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1"),
                                _sticker("UP-item0", "c2")]})
        with self.assertRaises(bc.SetDrift) as ctx:
            self._reconcile(tg, self._state_set())
        self.assertIn("twice", str(ctx.exception))

    def test_a_sticker_appended_by_the_owner_only_stops_attribution(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1"),
                                _sticker("OWNER", "cx")]})
        s = self._state_set()
        self.assertEqual(self._reconcile(tg, s), 3)
        self.assertEqual(s["keys"], self.keys)

    def test_replaced_sticker_is_drift(self):
        # Same length, but position 1 now holds item0's picture.
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item0", "cX")]})
        with self.assertRaises(bc.SetDrift):
            self._reconcile(tg, self._state_set())

    def test_matching_manifest_reconciles_the_tail(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1")]})
        s = self._state_set(keys=[self.keys[0]])   # item1 uploaded, not recorded
        self.assertEqual(self._reconcile(tg, s), 2)
        self.assertEqual(s["keys"], self.keys)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertTrue(cat.is_published("pk", self.keys[1]))

    def test_cids_are_never_written_from_a_drifted_position(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item1", "wrong-0"),
                                _sticker("UP-item0", "wrong-1")]})
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift):
                bc._record_cids(tg, cat, [self._state_set()], "pk")
            self.assertIsNone(cat.get(self.keys[0]).custom_emoji_id)
            self.assertIsNone(cat.get(self.keys[1]).custom_emoji_id)

    def test_cids_are_written_when_the_manifest_matches(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1")]})
        with Catalog(self.data / "catalog.db") as cat:
            bc._record_cids(tg, cat, [self._state_set()], "pk")
            self.assertEqual(cat.get(self.keys[0]).custom_emoji_id, "c0")
            self.assertEqual(cat.get(self.keys[1]).custom_emoji_id, "c1")


# --------------------------------------------------------------------------- #
# H-05 / M-01 / M-02 / M-03: CLI contracts
# --------------------------------------------------------------------------- #
def _main(*argv: str) -> int:
    """Run build_collection.main without touching .env or the log directory."""
    with mock.patch.object(bc, "load_env", lambda: None), \
            mock.patch.object(bc, "setup_logging", lambda *a, **k: None):
        return bc.main(list(argv))


class CliContract(_CatalogFixture):
    def _dry_run(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = _main("--base", "pk", "--title", "T", "--data-dir",
                       str(self.data), "--dry-run", *extra)
        return rc, out.getvalue()

    def test_second_publisher_fails_fast_while_the_lock_is_held(self):
        with exclusive_lock(bc._lock_path(self.data, "pk")):
            rc, _ = self._dry_run()
        self.assertEqual(rc, EXIT_FAILED)

    def test_the_lock_is_released_again(self):
        self.assertEqual(self._dry_run()[0], EXIT_OK)
        self.assertEqual(self._dry_run()[0], EXIT_OK)
        self.assertFalse(bc._lock_path(self.data, "pk").exists())

    def test_unknown_format_is_a_usage_error(self):
        self.assertEqual(self._dry_run("--formats", "garbage")[0], EXIT_USAGE)

    def test_duplicate_and_empty_formats_are_usage_errors(self):
        self.assertEqual(self._dry_run("--formats", "static,static")[0], EXIT_USAGE)
        self.assertEqual(self._dry_run("--formats", "static,")[0], EXIT_USAGE)
        self.assertEqual(self._dry_run("--formats", "")[0], EXIT_USAGE)

    def test_per_set_must_be_within_telegrams_cap(self):
        self.assertEqual(self._dry_run("--per-set", "0")[0], EXIT_USAGE)
        self.assertEqual(self._dry_run("--per-set", "-5")[0], EXIT_USAGE)
        self.assertEqual(self._dry_run("--per-set", "201")[0], EXIT_USAGE)

    def test_dry_run_counts_the_brand_logo_slot(self):
        # 198 planned + the 2 catalogued items = 200 emoji. With a logo in the
        # first slot of every set that is two sets, not one.
        bc.save_json(bc._plan_path(self.data, "pk"),
                     {"static": [f"s:x{i:030d}" for i in range(198)]})
        logo = self.data / "logo.png"
        _make_png(logo)
        rc, out = self._dry_run("--formats", "static", "--brand-logo", str(logo))
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("200 emoji -> 2 set(s) of up to 199", out)
        rc, out = self._dry_run("--formats", "static", "--no-brand-logo")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("200 emoji -> 1 set(s) of up to 200", out)

    def test_per_set_1_with_a_logo_is_rejected_not_a_zero_division(self):
        logo = self.data / "logo.png"
        _make_png(logo)
        self.assertEqual(
            self._dry_run("--per-set", "1", "--brand-logo", str(logo))[0],
            EXIT_USAGE)


# --------------------------------------------------------------------------- #
# M-04: a video is blank only if EVERY sampled frame is
# --------------------------------------------------------------------------- #
def _frame(visible: int) -> bytes:
    px = bytearray(bc._FRAME_BYTES)
    for i in range(visible):
        px[i * 4 + 3] = 255
    return bytes(px)


class VideoBlankCheck(unittest.TestCase):
    def _media_ok(self, raw: bytes) -> bool:
        run = mock.Mock(return_value=SimpleNamespace(stdout=raw, returncode=0))
        # Patch on the subprocess module itself: the old first-frame probe lived
        # in emojikit.media, so a build_collection-only patch would not bind it.
        with mock.patch("subprocess.run", run), \
                mock.patch.object(bc.media, "ffmpeg_path", lambda: "ffmpeg"):
            return bc._media_ok(Path("clip.webm"), "video")

    def test_fade_in_video_is_accepted(self):
        raw = _frame(0) + _frame(0) + _frame(500) + _frame(900)
        self.assertTrue(self._media_ok(raw))

    def test_fully_blank_video_is_still_rejected(self):
        self.assertFalse(self._media_ok(_frame(0) * 4))

    def test_a_handful_of_stray_pixels_is_still_blank(self):
        self.assertFalse(self._media_ok(_frame(3) * 3))

    def test_undecodable_output_lets_the_upload_decide(self):
        self.assertTrue(self._media_ok(b""))


if __name__ == "__main__":
    unittest.main()
