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
  permanent skip;
* a recorded position holding an identity we had NEVER seen passed the
  manifest check on order alone, and then received our custom_emoji_id;
* an unattributed sticker in the live tail only stopped attribution, so the
  next publish appended past it and mapped a new key onto its cid;
* the blank-video probe ran ffmpeg with no timeout at all;
* a run where every upload failed still exited 0;
* an older recorded set that was MISSING or UNKNOWN was warned about and the
  run reported a clean DONE.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import build_collection as bc  # noqa: E402
import build_pack as bp  # noqa: E402
from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE,  # noqa: E402
                        LiveStateUnknown, SetState, announce_packs, exclusive_lock)
from emojikit import media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

SET = "pks1_by_YourEmojiBot"
SET2 = "pks2_by_YourEmojiBot"


def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(20, 80):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


class FakeTG:
    """In-memory Telegram with the tri-state probe the real client exposes."""

    def __init__(self, sets=None, unknown=(), fail_after=None):
        self.sets: dict[str, list[dict]] = dict(sets or {})
        self.unknown = set(unknown)          # names whose live state is unknown
        self.fail_after = fail_after         # uploads accepted before it breaks
        self.uploaded: list[str] = []
        self.sent: list[str] = []
        # file_unique_id -> the bytes that were uploaded for it, so a later
        # download can prove the live sticker is the one we sent.
        self.bodies: dict[str, bytes] = {}
        # Which stickers were actually fetched: content attribution is the
        # expensive route, so its bound has to be assertable.
        self.downloaded: list[str] = []
        # file_unique_ids whose fetch FAILS -- "I could not look", as distinct
        # from "I looked and it is not ours".
        self.undownloadable: set[str] = set()

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

    # ----- publishing side (the uploaded copy gets its own file_unique_id) --- #
    def get_me(self):
        return {"username": "YourEmojiBot"}

    # Telegram re-encodes on upload, so the live copy's file_unique_id is a
    # FRESH one the catalog has never seen (``fuid_prefix``). The fixture
    # pre-records "UP-item<i>", so a fake that returns those ids would hide the
    # very window these tests are about.
    fuid_prefix = "UP-"

    def _new(self, name: str, path) -> dict:
        if self.fail_after is not None and len(self.uploaded) >= self.fail_after:
            raise RuntimeError("BAD_REQUEST: STICKER_PNG_DIMENSIONS")
        stem = Path(path).stem
        self.uploaded.append(stem)
        st = _sticker(f"{self.fuid_prefix}{stem}",
                      f"{name}-{len(self.sets.get(name, []))}")
        # Remember what was uploaded: the publisher now PROVES the new sticker
        # is ours by downloading it and hashing its content, so a fake that
        # cannot serve the bytes back makes every upload "unidentifiable".
        self.bodies[st["file_unique_id"]] = Path(path).read_bytes()
        return st

    def download_file(self, file_id, dest):
        """Serve the sticker's bytes, as Telegram would.

        A sticker nobody uploaded through this fake is still a REAL, fetchable
        image -- it just is not in our catalog. Raising here instead would make
        "someone else's sticker" and "the download failed" the same event, and
        those are the two cases the publisher must tell apart: the first is a
        proven negative, the second is no answer at all. Use ``undownloadable``
        for the genuine failure.
        """
        fuid = str(file_id)[2:] if str(file_id).startswith("f-") else str(file_id)
        self.downloaded.append(fuid)
        if fuid in self.undownloadable:
            raise RuntimeError(f"download failed: {file_id}")
        body = self.bodies.get(fuid)
        if body is None:
            # Deterministic per-fuid art so two foreign stickers never collide
            # onto one content key, which would read as a duplicate of ours.
            seed = sum(fuid.encode()) % 200
            foreign = Path(dest).with_suffix(".foreign.png")
            _make_png(foreign, color=(seed, 255 - seed, 90, 255))
            body = foreign.read_bytes()
            foreign.unlink(missing_ok=True)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(body)
        return dest

    def create_emoji_set(self, user_id, name, title, path, fmt, emojis, keywords):
        self.sets[name] = [self._new(name, path)]

    def add_emoji(self, user_id, name, path, fmt, emojis, keywords, *,
                  expected_before=None):
        self.sets[name].append(self._new(name, path))

    def send_message(self, chat_id, text, *, disable_preview=False):
        self.sent.append(text)


class DownloadingTG(FakeTG):
    """FakeTG that also serves downloads, so CONTENT attribution really runs."""

    def __init__(self, *a, error: str | None = None, **kw):
        super().__init__(*a, **kw)
        self.error = error
        self.downloads = 0

    def download_file(self, file_id, dest):
        self.downloads += 1
        if self.error:
            raise RuntimeError(self.error)
        Path(dest).write_bytes(b"whatever telegram returned")
        return dest


def _sticker(fuid: str, cid: str) -> dict:
    # file_id is what _resolve_sticker_key needs before it will download at all.
    return {"file_unique_id": fuid, "custom_emoji_id": cid, "file_id": f"f-{fuid}"}


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
                # The REAL content key, not a synthetic one: publishing now
                # attributes a live sticker by downloading it and hashing the
                # pixels, so a made-up key could never match and every upload
                # would look unidentifiable.
                key = media.content_key(p, "static")
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

    def _add_item(self, i: int) -> str:
        """Catalogue one more static item, as a later curate pass would."""
        p = self.data / "media" / "static" / f"item{i}.png"
        _make_png(p, color=(10, 30 * (i + 1), 90, 255))
        key = media.content_key(p, "static")
        with Catalog(self.data / "catalog.db") as cat:
            cat.add(content_key=key, fmt="static", file_path=p,
                    emojis=["\U0001F600"], keywords=[f"item{i}"])
            cat.record_file_unique_id(f"UP-item{i}", key)
        return key

    def _read_back(self, *keys: str) -> None:
        """Pretend a previous run already stored these keys' custom_emoji_ids.

        That is the moment a position's live identity becomes known, and it is
        what makes an unknown identity there proof of a replacement.
        """
        with Catalog(self.data / "catalog.db") as cat:
            for i, key in enumerate(keys):
                cat.mark_uploaded(key, f"c{i}", base="pk", set_name=SET)


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

    # ----- M-11: the WHOLE shape is checked, before anything mutates ------- #
    def _bad_state(self, *sets, **top) -> None:
        state = {"base": "pk", "sets": list(sets), "sent": []}
        state.update(top)
        bc.save_json(bc._state_path(self.data, "pk"), state)

    def _set(self, **over) -> dict:
        s = {"fmt": "static", "index": 1, "name": SET, "title": "Pack 1",
             "live": 2, "logo": False, "keys": list(self.keys)}
        s.update(over)
        return s

    def test_a_wholly_consistent_state_is_accepted(self):
        self._bad_state(self._set())
        self.assertEqual(len(bc.load_state(self.data, "pk")["sets"]), 1)

    def test_the_same_index_in_two_formats_is_fine(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(fmt="video", name="pkv1_by_bot", live=1,
                                  keys=[self.keys[1]]))
        self.assertEqual(len(bc.load_state(self.data, "pk")["sets"]), 2)

    def test_a_negative_live_count_is_rejected(self):
        self._bad_state(self._set(live=-1, keys=[]))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_a_live_count_above_telegrams_cap_is_rejected(self):
        self._bad_state(self._set(live=201, keys=[]))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_more_recorded_keys_than_live_stickers_is_rejected(self):
        # The cid mapping reads live[i + offset] for every key, so this state
        # would index past the end -- or worse, onto somebody else's sticker.
        self._bad_state(self._set(live=1))
        with self.assertRaises(bc.StateError) as ctx:
            bc.load_state(self.data, "pk")
        self.assertIn("records 2", str(ctx.exception))

    def test_the_logo_slot_counts_towards_that_bound(self):
        self._bad_state(self._set(live=2, logo=True))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_one_emoji_recorded_twice_in_a_set_is_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0], self.keys[0]]))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_one_emoji_recorded_in_two_sets_is_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(index=2, name=SET2, live=1,
                                  keys=[self.keys[0]]))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_two_sets_sharing_a_name_are_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(index=2, keys=[self.keys[1]], live=1))
        with self.assertRaises(bc.StateError):
            bc.load_state(self.data, "pk")

    def test_a_repeated_or_backwards_index_is_rejected(self):
        for second in ({"index": 1}, {"index": 0}):
            self._bad_state(self._set(index=2, keys=[self.keys[0]], live=1),
                            self._set(name=SET2, keys=[self.keys[1]], live=1,
                                      **second))
            with self.assertRaises(bc.StateError):
                bc.load_state(self.data, "pk")

    def test_malformed_field_types_are_rejected(self):
        for over in ({"keys": "not-a-list"}, {"keys": [1, 2]}, {"logo": "yes"},
                     {"live": "two"}, {"live": True}, {"index": True},
                     {"title": ""}, {"name": ""}):
            self._bad_state(self._set(**over))
            with self.assertRaises(bc.StateError):
                bc.load_state(self.data, "pk")

    def test_malformed_sent_and_skipped_entries_are_rejected(self):
        for top in ({"sent": [None]}, {"sent": [""]}, {"skipped": [{"k": 1}]}):
            self._bad_state(**top)
            with self.assertRaises(bc.StateError):
                bc.load_state(self.data, "pk")

    def test_a_broken_state_stops_the_run_before_any_mutation(self):
        self._bad_state(self._set(live=1))       # records more than it holds
        tg = FakeTG()
        with mock.patch.object(bc, "Telegram", lambda token: tg), \
                mock.patch.dict(os.environ, {"GENERAL_BOT_TOKEN": "x"}), \
                redirect_stdout(io.StringIO()):
            rc = _main("--base", "pk", "--title", "T", "--user-id", "7",
                       "--formats", "static", "--no-brand-logo",
                       "--data-dir", str(self.data))
        self.assertEqual(rc, EXIT_FAILED)
        self.assertEqual(tg.uploaded, [])        # no Telegram mutation at all
        self.assertEqual(tg.sets, {})
        # ...not even the frozen plan was rewritten from the bad state.
        self.assertFalse(bc._plan_path(self.data, "pk").exists())

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
                bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
            self.assertIsNone(cat.get(self.keys[0]).custom_emoji_id)
            self.assertIsNone(cat.get(self.keys[1]).custom_emoji_id)

    def test_cids_are_written_when_the_manifest_matches(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1")]})
        with Catalog(self.data / "catalog.db") as cat:
            bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
            self.assertEqual(cat.get(self.keys[0]).custom_emoji_id, "c0")
            self.assertEqual(cat.get(self.keys[1]).custom_emoji_id, "c1")


# --------------------------------------------------------------------------- #
# C-03: an identity we have NEVER seen may not hold a recorded position
# --------------------------------------------------------------------------- #
class ForeignIdentityOnARecordedPosition(_CatalogFixture):
    """The old check only rejected a file_unique_id that was already known and
    mapped elsewhere. A never-seen foreign sticker returned ``known is None``
    and passed, after which its custom_emoji_id was written onto our key."""

    def test_a_foreign_sticker_on_a_read_back_position_is_drift(self):
        self._read_back(*self.keys)          # a previous run learned both ids
        tg = FakeTG(sets={SET: [_sticker("NEVER-SEEN", "foreign-cid"),
                                _sticker("UP-item1", "c1")]})
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift) as ctx:
                bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
            self.assertIn("position 0", str(ctx.exception))
            # The foreign sticker's id never reached our item.
            self.assertEqual(cat.custom_emoji_id_for("pk", self.keys[0]), "c0")

    def test_reconcile_also_refuses_a_replaced_recorded_position(self):
        self._read_back(*self.keys)
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("NEVER-SEEN", "foreign-cid")]})
        s = self._state_set()
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift):
                bc.reconcile_set(tg, cat, s, self.data, "pk")

    def test_content_resolution_rescues_a_re_uploaded_identical_picture(self):
        # A new id is not automatically a different emoji: if the bytes still
        # resolve to the recorded key it is the same picture, not drift. A
        # re-upload is a NEW sticker, so its custom_emoji_id is new too and only
        # the content can settle it.
        self._read_back(*self.keys)
        tg = DownloadingTG(sets={SET: [_sticker("RE-UPLOADED", "c9"),
                                       _sticker("UP-item1", "c1")]})
        with mock.patch.object(bc.media, "content_key",
                               lambda p, fmt: self.keys[0]), \
                mock.patch.object(bc.media, "telegram_sticker_format",
                                  lambda st: "static"):
            with Catalog(self.data / "catalog.db") as cat:
                bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
        self.assertEqual(tg.downloads, 1)

    def test_a_fresh_upload_whose_identity_was_recorded_is_not_drift(self):
        # Telegram re-encodes on upload, so the copy's id is only learnable by
        # reading the set back -- which _confirm_new_upload does at the moment
        # of the upload. Once recorded, the position resolves by identity.
        with Catalog(self.data / "catalog.db") as cat:
            # strict: if the fixture ever stops having exactly two keys, this
            # records fewer fuids than it names and the assertions below pass
            # without having exercised the case.
            for fuid, key in zip(("BRAND-NEW-0", "BRAND-NEW-1"), self.keys,
                                 strict=True):
                cat.record_file_unique_id(fuid, key)
        tg = FakeTG(sets={SET: [_sticker("BRAND-NEW-0", "c0"),
                                _sticker("BRAND-NEW-1", "c1")]})
        with Catalog(self.data / "catalog.db") as cat:
            bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
            self.assertEqual(cat.custom_emoji_id_for("pk", self.keys[0]), "c0")

    def test_an_unidentifiable_fresh_position_never_falls_back_to_order(self):
        """C-02: the window that let ``sol`` inherit a Solama llama.

        State from a run that uploaded both emoji but had not read the set back
        yet -- no custom_emoji_id stored for either key. The live set was
        reordered in the meantime; it is the SAME LENGTH, so only identity can
        tell. Neither id is one this publisher recorded, so there is nothing to
        trust and the ids must not be written from position.
        """
        tg = FakeTG(sets={SET: [_sticker("FRESH-1", "c1"),
                                _sticker("FRESH-0", "c0")]})
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift):
                bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
            self.assertIsNone(cat.custom_emoji_id_for("pk", self.keys[0]))
            self.assertIsNone(cat.custom_emoji_id_for("pk", self.keys[1]))

    def test_content_resolution_failure_is_drift_not_a_positional_guess(self):
        # The download is the last identity route; when it fails the position
        # stays unproven, and unproven must never mean "order held".
        tg = DownloadingTG(sets={SET: [_sticker("FRESH-0", "c0"),
                                       _sticker("FRESH-1", "c1")]},
                           error="connection reset")
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift):
                bc._record_cids(tg, cat, [self._state_set()], "pk", self.data)
        self.assertEqual(tg.downloads, 1)   # tried identity first, then stopped


# --------------------------------------------------------------------------- #
# C-04: a set with an unattributed live position is closed for publishing
# --------------------------------------------------------------------------- #
class UnattributedTail(_CatalogFixture):
    def test_a_tail_we_cannot_attribute_closes_the_set(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("OWNER", "cx")]})
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(bc.reconcile_set(tg, cat, s, self.data, "pk"), 2)
        self.assertEqual(s["keys"], [self.keys[0]])   # the tail stayed unowned
        self.assertFalse(bc._set_is_open(s))

    def test_a_tail_whose_download_fails_is_refused_not_closed(self):
        """Closing the set on a failed fetch is how the emoji gets duplicated.

        Content is the only remaining route for an unrecorded tail sticker. If
        the fetch fails and the set is merely closed, publishing rolls to a new
        set -- and when that sticker WAS ours, its emoji is now live twice. The
        run that left it unrecorded usually died of a network fault, so this is
        the correlated case, not an exotic one. An unreadable position has to be
        a refusal.
        """
        tg = DownloadingTG(sets={SET: [_sticker("UP-item0", "c0"),
                                       _sticker("UNSEEN", "cx")]},
                           error="connection reset")
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift) as ctx:
                bc.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertIn("could not be examined", str(ctx.exception))
        self.assertEqual(tg.downloads, 1)
        self.assertEqual(s["keys"], [self.keys[0]])   # nothing was recorded

    def test_an_unreadable_sticker_behind_a_foreign_one_is_not_read_as_absent(self):
        """The look-behind must not report "nothing of ours" when it could not look.

        Layout [ours, FOREIGN, ours-but-unrecorded] with the last one's fetch
        failing. Treating that failure as absence breaks the loop, closes the
        set, and publishing rolls to a new one -- a second live copy of the same
        emoji, silently. This is the whole defect, reached through the error path
        instead of through the id lookup it replaced.
        """
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("OWNER", "cx"),
                                _sticker("FRESH-item1", "c2")]})
        tg.undownloadable.add("FRESH-item1")
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift) as ctx:
                bc.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertIn("could not be examined", str(ctx.exception))
        self.assertEqual(s["keys"], [self.keys[0]])

    def test_a_fully_attributed_set_stays_open(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1")]})
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            bc.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertTrue(bc._set_is_open(s))


# --------------------------------------------------------------------------- #
# H-01: an emoji of OURS hiding behind a foreign sticker is found by CONTENT
# --------------------------------------------------------------------------- #
class OurEmojiBehindAForeignSticker(_CatalogFixture):
    """The look-behind used to be an id lookup only.

    A sticker this program uploaded moments before the run died never got its
    file_unique_id recorded, so ``seen_file_unique_id`` never heard of it: the
    look-behind answered "nothing of ours behind", attribution stopped quietly,
    and the emoji stayed live-but-pending -- i.e. uploaded again, into a new
    set, on the next run. Exactly the duplicate this project exists to prevent.
    """

    def _unrecorded(self, tg: FakeTG, i: int) -> dict:
        """A live sticker holding item<i>'s pixels, with an id nobody recorded.

        Only a download can see it: the catalog knows the CONTENT, never this
        copy's Telegram identity.
        """
        st = _sticker(f"FRESH-item{i}", f"fresh-c{i}")
        tg.bodies[st["file_unique_id"]] = (
            self.data / "media" / "static" / f"item{i}.png").read_bytes()
        return st

    def test_an_unrecorded_upload_behind_a_foreign_sticker_is_refused(self):
        # [..ours.., FOREIGN, OURS_UNRECORDED]
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("OWNER", "owner-cid")]})
        tg.sets[SET].append(self._unrecorded(tg, 1))
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift) as ctx:
                bc.reconcile_set(tg, cat, s, self.data, "pk")
            # Still pending would mean re-uploaded; the run refuses instead.
            self.assertFalse(cat.is_published("pk", self.keys[1]))
        self.assertIn(self.keys[1], str(ctx.exception))
        self.assertIn("position 2", str(ctx.exception))
        self.assertEqual(s["keys"], [self.keys[0]])  # never attributed by order

    def test_the_look_behind_stops_at_the_first_of_ours(self):
        # Bound: the rest of the tail at worst, and not one download further
        # than the sticker that proves the set was edited by hand.
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("OWNER", "owner-cid")]})
        tg.sets[SET] += [self._unrecorded(tg, 1), _sticker("OWNER2", "owner-cid2")]
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            with self.assertRaises(bc.SetDrift):
                bc.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertEqual(tg.downloaded, ["OWNER", "FRESH-item1"])

    def test_a_foreign_sticker_at_the_very_end_still_stops_quietly(self):
        # The case the id-only check got right, and the reason this cannot just
        # raise on every unrecognized sticker: nothing of ours is behind it, so
        # the set is merely closed and publishing rolls to a fresh one.
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1"),
                                _sticker("OWNER", "owner-cid")]})
        s = self._state_set(live=2)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(bc.reconcile_set(tg, cat, s, self.data, "pk"), 3)
        self.assertEqual(s["keys"], self.keys)
        self.assertFalse(bc._set_is_open(s))
        self.assertEqual(tg.downloaded, ["OWNER"])   # nothing past the stop


# --------------------------------------------------------------------------- #
# C-02: the fresh-upload window, end to end through the publisher
# --------------------------------------------------------------------------- #
class FreshUploadTG(FakeTG):
    """Uploads come back with identities the catalog has never seen.

    That is what Telegram really does (it re-encodes, so the live copy gets its
    own file_unique_id), and it is the state in which a set edit used to be
    invisible: nothing to compare against, so order was believed.
    """

    fuid_prefix = "FRESH-"


class FreshUploadIdentity(_CatalogFixture):
    def _publish(self, tg, edit=None) -> int:
        """Publish both items; ``edit(tg.sets)`` runs in the window between the
        last upload and the read-back that assigns custom_emoji_ids."""
        record_cids = bc._record_cids

        def edited(*a, **kw):
            if edit:
                edit(tg.sets)
            return record_cids(*a, **kw)

        with mock.patch.object(bc, "Telegram", lambda token: tg), \
                mock.patch.object(bc, "_record_cids", edited), \
                mock.patch.object(bc.time, "sleep", lambda s: None), \
                mock.patch.dict(os.environ, {"GENERAL_BOT_TOKEN": "x",
                                             "PACK_LINKS_CHAT_ID": ""}):
            with redirect_stdout(io.StringIO()):
                return _main("--base", "pk", "--title", "Pack", "--formats",
                             "static", "--user-id", "7", "--no-brand-logo",
                             "--data-dir", str(self.data))

    def test_a_same_length_reorder_before_read_back_fails_closed(self):
        """Both emoji are ours, so lengths, counts and set membership all still
        match -- only identity notices. Believing order here is exactly how a
        ticker ended up on another project's artwork."""
        tg = FreshUploadTG()
        self.assertEqual(self._publish(tg, lambda sets: sets[SET].reverse()),
                         EXIT_FAILED)
        with Catalog(self.data / "catalog.db") as cat:
            # item0 keeps the id of the sticker that was identified as its own
            # at upload time, and never the one that moved into its position.
            self.assertEqual(cat.custom_emoji_id_for("pk", self.keys[0]),
                             f"{SET}-0")
            self.assertEqual(cat.custom_emoji_id_for("pk", self.keys[1]),
                             f"{SET}-1")

    def test_a_foreign_sticker_swapped_in_never_takes_our_key(self):
        tg = FreshUploadTG()

        def swap(sets):
            sets[SET][0] = _sticker("NEVER-SEEN", "foreign-cid")

        self.assertEqual(self._publish(tg, swap), EXIT_FAILED)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertNotEqual(cat.custom_emoji_id_for("pk", self.keys[0]),
                                "foreign-cid")
            self.assertEqual(cat.custom_emoji_id_for("pk", self.keys[0]),
                             f"{SET}-0")

    def test_the_uploaded_copys_identity_is_recorded_at_upload_time(self):
        tg = FreshUploadTG()
        self.assertEqual(self._publish(tg), EXIT_OK)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.seen_file_unique_id("FRESH-item0"), self.keys[0])
            self.assertEqual(cat.seen_file_unique_id("FRESH-item1"), self.keys[1])

    def test_an_upload_that_adds_no_identity_is_never_recorded(self):
        """The API said yes but nothing new is live: there is no sticker to
        attribute, so the key must stay pending rather than claim a position."""
        class SilentTG(FreshUploadTG):
            def add_emoji(self, *a, **kw):
                pass                      # accepted, but nothing appears

        tg = SilentTG()
        self.assertEqual(self._publish(tg), EXIT_FAILED)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertTrue(cat.is_published("pk", self.keys[0]))
            self.assertFalse(cat.is_published("pk", self.keys[1]))

    def test_a_concurrent_writer_makes_the_position_ambiguous(self):
        """Two new stickers, one of them somebody else's: which is ours is a
        guess, and a guess is not identity."""
        class RacedTG(FreshUploadTG):
            def add_emoji(self, user_id, name, path, fmt, emojis, keywords, *,
                          expected_before=None):
                super().add_emoji(user_id, name, path, fmt, emojis, keywords,
                                  expected_before=expected_before)
                self.sets[name].append(_sticker("SOMEONE-ELSE", "other-cid"))

        tg = RacedTG()
        self.assertEqual(self._publish(tg), EXIT_FAILED)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertFalse(cat.is_published("pk", self.keys[1]))
            self.assertIsNone(cat.seen_file_unique_id("SOMEONE-ELSE"))


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


class OccupiedNameTG(FakeTG):
    """The set name is already taken by a LIVE set, so the publisher adopts it.

    Adoption is the only route to the reconcile that runs inside the upload
    try-block -- the one place a drift refusal used to be caught as an upload
    that merely failed.
    """

    def create_emoji_set(self, user_id, name, title, path, fmt, emojis, keywords):
        raise RuntimeError("BAD_REQUEST: sticker set name is already occupied")


class PublishThroughMain(_CatalogFixture):
    """The real entry point: publish, then resume without re-uploading."""

    def _run(self, tg, *extra: str) -> int:
        with mock.patch.object(bc, "Telegram", lambda token: tg), \
                mock.patch.object(bc.time, "sleep", lambda s: None), \
                mock.patch.dict(os.environ, {"GENERAL_BOT_TOKEN": "x",
                                             "PACK_LINKS_CHAT_ID": ""}):
            return _main("--base", "pk", "--title", "Pack", "--formats", "static",
                         "--user-id", "7", "--no-brand-logo",
                         "--data-dir", str(self.data), *extra)

    def test_publish_then_resume_uploads_each_emoji_once(self):
        tg = FakeTG()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self._run(tg), EXIT_OK)
        self.assertEqual(tg.uploaded, ["item0", "item1"])

        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(state["sets"][0]["keys"], self.keys)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.get(self.keys[0]).custom_emoji_id, f"{SET}-0")
            self.assertEqual(cat.get(self.keys[1]).custom_emoji_id, f"{SET}-1")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        self.assertEqual(tg.uploaded, ["item0", "item1"])   # nothing re-uploaded
        self.assertEqual(len(tg.sets[SET]), 2)

    def test_a_set_emptied_between_runs_stops_the_resume(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        tg.sets[SET] = []                     # owner emptied the pack by hand
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_FAILED)
        self.assertEqual(tg.uploaded, ["item0", "item1"])

    def test_unreachable_telegram_is_retryable_not_a_failure(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        tg.unknown.add(SET)                   # network down on the next run
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_PARTIAL)
        self.assertEqual(tg.uploaded, ["item0", "item1"])

    # ----- C-04: publishing never appends behind a foreign sticker -------- #
    def test_a_foreign_tail_sticker_sends_the_next_emoji_to_a_new_set(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        # The owner appends a sticker of their own; then a third emoji is
        # catalogued. keys[] has no slot for the foreign sticker, so appending
        # to this set would hand item2 the owner's custom_emoji_id.
        tg.sets[SET].append(_sticker("OWNER", "owner-cid"))
        key2 = self._add_item(2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.custom_emoji_id_for("pk", key2), f"{SET2}-0")
        self.assertEqual(len(tg.sets[SET]), 3)        # pks1 was left alone
        self.assertEqual(len(tg.sets[SET2]), 1)

    # ----- H-11: every recorded set is verified, not just the active one -- #
    def test_an_older_recorded_set_that_disappeared_fails_closed(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "1"), EXIT_OK)
        self.assertEqual(sorted(tg.sets), [SET, SET2])
        del tg.sets[SET]                      # owner deleted the FIRST pack
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "1"), EXIT_FAILED)

    def test_an_older_set_that_cannot_be_read_is_retryable(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "1"), EXIT_OK)
        tg.unknown.add(SET)                   # transient: never a clean DONE
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "1"), EXIT_PARTIAL)

    # ----- H-10: a run that uploaded nothing is not a success ------------- #
    def test_a_run_where_every_upload_failed_exits_non_zero(self):
        tg = FakeTG(fail_after=0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_FAILED)
        self.assertEqual(tg.uploaded, [])

    def test_a_partly_failed_run_is_partial(self):
        tg = FakeTG(fail_after=1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_PARTIAL)
        self.assertEqual(tg.uploaded, ["item0"])

    # ----- OI-1: a drift REFUSAL is never a retryable upload failure ------ #
    def test_drift_while_adopting_a_set_stops_the_run_instead_of_retrying(self):
        """The reconcile of an adopted set runs INSIDE the upload try-block, so
        its refusal used to arrive at the generic "upload failed (will retry)"
        handler. Carrying on rolled ``set_index`` back over the set record just
        appended, adopted the same name again and wrote TWO sets under one name
        -- state the next run's ``load_state`` refuses outright, so the pack
        family could never be published again.
        """
        # A live set already holds the name, and its one sticker cannot be
        # fetched: adoption succeeds, then the reconcile must refuse rather than
        # guess whether that position is ours.
        tg = OccupiedNameTG(sets={SET: [_sticker("GHOST", "ghost-cid")]})
        tg.undownloadable = {"GHOST"}
        out = io.StringIO()
        with self.assertLogs("build_collection", "WARNING") as logs, \
                redirect_stdout(out):
            self.assertEqual(self._run(tg), EXIT_FAILED)

        logged = "\n".join(logs.output)
        # Pins the branch under test: only reconcile_set's unreadable-position
        # refusal words it this way, and it is reached from the adopt path.
        self.assertIn("Refusing to decide whether it is ours", logged)
        self.assertNotIn("will retry", logged)
        self.assertNotIn("DONE", out.getvalue())    # stopped, not "finished"

        state = json.loads(
            bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in state["sets"]], [SET])
        bc.load_state(self.data, "pk")              # a later run can still start

    def test_skipped_blank_media_is_not_counted_as_a_failure(self):
        # A permanent, recorded exclusion is not retryable work: the run that
        # records it is still a success.
        tg = FakeTG()
        blank = self.data / "media" / "static" / "blank.png"
        _make_png(blank, color=(0, 0, 0, 0))
        key = "s:blank" + "0" * 25
        with Catalog(self.data / "catalog.db") as cat:
            cat.add(content_key=key, fmt="static", file_path=blank,
                    emojis=["\U0001F600"], keywords=["blank"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        self.assertEqual(tg.uploaded, ["item0", "item1"])


# --------------------------------------------------------------------------- #
# M-04: a video is blank only if EVERY sampled frame is
# --------------------------------------------------------------------------- #
def _frame(visible: int) -> bytes:
    px = bytearray(bc._FRAME_BYTES)
    for i in range(visible):
        px[i * 4 + 3] = 255
    return bytes(px)


def _fake_popen(raw: bytes, *, hang: bool = False):
    """A Popen stand-in recording the wall limit each child was given.

    Patched at ``subprocess.Popen`` on purpose: both the old bare
    ``subprocess.run`` and the bounded ``media._run`` go through it, so the
    recorded timeout is a fair comparison between them.
    """
    seen: list = []

    class _P:
        def __init__(self, cmd, stdout=None, stderr=None, **kw):
            self.args, self.returncode = cmd, 0

        def communicate(self, input=None, timeout=None):
            seen.append(timeout)
            if hang:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            return raw, b""

        def poll(self):
            return self.returncode

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    return _P, seen


class VideoBlankCheck(unittest.TestCase):
    def _probe(self, raw: bytes, *, hang: bool = False):
        popen, seen = _fake_popen(raw, hang=hang)
        with mock.patch("subprocess.Popen", popen), \
                mock.patch.object(bc.media, "ffmpeg_path", lambda: "ffmpeg"):
            return bc._media_ok(Path("clip.webm"), "video"), seen

    def _media_ok(self, raw: bytes) -> bool:
        return self._probe(raw)[0]

    # ----- H-09: the probe is bounded like every other ffmpeg child ------- #
    def test_the_probe_runs_under_a_finite_wall_limit(self):
        ok, seen = self._probe(_frame(900) * 2)
        self.assertTrue(ok)
        self.assertTrue(seen, "ffmpeg was never started")
        self.assertTrue(all(t and t > 0 for t in seen),
                        f"unbounded ffmpeg child: timeouts={seen}")

    def test_a_hanging_ffmpeg_is_killed_and_lets_the_upload_decide(self):
        self.assertTrue(self._probe(b"", hang=True)[0])

    def test_fade_in_video_is_accepted(self):
        raw = _frame(0) + _frame(0) + _frame(500) + _frame(900)
        self.assertTrue(self._media_ok(raw))

    def test_fully_blank_video_is_still_rejected(self):
        self.assertFalse(self._media_ok(_frame(0) * 4))

    def test_a_handful_of_stray_pixels_is_still_blank(self):
        self.assertFalse(self._media_ok(_frame(3) * 3))

    def test_undecodable_output_lets_the_upload_decide(self):
        self.assertTrue(self._media_ok(b""))


class PackTitlesAreOneSequence(unittest.TestCase):
    """Titles read "<title> 1, 2, 3" across every format, not per format.

    They used to be "<title> Animated 1" / "<title> Static 1" -- three separate
    sequences, so two packs both called "1". The number now counts every set
    already created, which also makes it resumable: a restarted run continues
    the count instead of restarting it.
    """

    def test_the_number_counts_all_formats_not_just_this_one(self):
        src = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertIn("len(state['sets']) + 1", src)
        self.assertNotIn("FMT_WORD", src,
                         "a format word in the title reintroduces the split")

    def test_the_announcement_uses_the_recorded_title(self):
        """Rebuilding the title at announce time is how it drifts from the set."""
        src = Path(bc.__file__).read_text(encoding="utf-8")
        block = src[src.index("if in_set >= per_set:"):]
        block = block[:block.index("time.sleep")]
        self.assertIn('fmt_sets[-1]["title"]', block)


if __name__ == "__main__":
    unittest.main()


class EveryPublisherSharesOneAnnouncer(unittest.TestCase):
    """All three publishers announce through ``announce_packs``.

    They used to carry three copies of "format the link and sendMessage", and
    when the Worker arrived only the collector learned about it -- so a coin
    rebuild or a single-pack build kept talking to Telegram from this machine
    while the owner believed the bot was posting. These tests fail if any
    publisher grows its own copy again.
    """

    WORKER = {"WORKER_PUBLISH_URL": "https://w.dev/publish",
              "WORKER_PUBLISH_SECRET": "s"}

    def test_the_single_pack_build_and_the_coin_rebuild_both_import_it(self):
        import coins.rebuild_dedup as rd
        for mod in (bc, bp, rd):
            self.assertIs(mod.announce_packs, announce_packs,
                          f"{mod.__name__} does not use the shared announcer")

    def test_half_a_worker_config_takes_the_direct_path(self):
        # URL without secret is a half-finished setup. Routing to it anyway
        # would 401 every announcement; silently "succeeding" via the direct
        # path at least still posts, and the missing secret stays visible.
        tg = FakeTG()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False), \
             mock.patch.object(bp, "announce_via_worker") as worker:
            dest = announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}],
                                  bot="general")
        worker.assert_not_called()
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(dest, "7")

    def test_the_direct_path_disables_link_previews(self):
        # 30 addemoji links with a preview card each buries the list. The coin
        # script used to do this with a private _call; losing it in the move to
        # a shared announcer would be a silent regression.
        seen = []
        tg = mock.Mock()
        tg.send_message.side_effect = lambda *a, **kw: seen.append(kw)
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False):
            announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}], bot="coin")
        self.assertTrue(all(kw.get("disable_preview") for kw in seen), seen)

    def test_a_whole_family_goes_in_ONE_worker_call(self):
        # The Worker splits across messages when it passes 4096 characters; a
        # per-pack call would defeat that and also post 30 separate messages.
        tg = FakeTG()
        packs = [{"name": f"p{i}_by_bot", "title": str(i)} for i in range(30)]
        with mock.patch.dict(os.environ, self.WORKER, clear=False), \
             mock.patch.object(bp, "announce_via_worker") as worker:
            announce_packs(tg, 7, packs, bot="coin", note="all packs:")
        worker.assert_called_once()
        self.assertEqual(len(worker.call_args.args[0]), 30)
        self.assertEqual(worker.call_args.kwargs["note"], "all packs:")
        self.assertEqual(worker.call_args.kwargs["bot"], "coin")

    def test_the_note_is_sent_too_on_the_direct_path(self):
        tg = FakeTG()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False):
            announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}],
                           bot="coin", note="header")
        self.assertEqual(tg.sent[0], "header")
        self.assertIn("t.me/addemoji/a_by_bot", tg.sent[1])


class AnnouncementRoutesThroughTheWorker(unittest.TestCase):
    """With a Worker configured, the BOT posts the link -- not this process.

    The duplicate guard must not change with the route: `state["sent"]` is what
    stops a re-run announcing the same pack twice, and it has to hold whichever
    path did the sending.
    """

    PACK = "clos1_by_bot"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = {"sent": []}

    def tearDown(self):
        self.tmp.cleanup()

    def _notify(self, tg):
        bc.notify(tg, 1, self.state, self.dir, "clos", self.PACK, "Closure 1")

    def test_worker_is_used_and_telegram_is_not_touched(self):
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(bp, "announce_via_worker") as worker:
            self._notify(tg)
        worker.assert_called_once()
        packs = worker.call_args.args[0]
        self.assertEqual(packs[0]["name"], self.PACK)
        tg.send_message.assert_not_called()
        self.assertIn(self.PACK, self.state["sent"])

    def test_without_a_worker_it_still_posts_directly(self):
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": ""}, clear=False):
            self._notify(tg)
        tg.send_message.assert_called_once()
        self.assertIn(self.PACK, self.state["sent"])

    def test_a_failed_worker_call_does_not_record_it_as_sent(self):
        # Otherwise the pack is never announced: the guard would skip it forever.
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(bp, "announce_via_worker",
                               side_effect=RuntimeError("worker down")):
            self._notify(tg)
        self.assertEqual(self.state["sent"], [])

    def test_an_already_sent_pack_is_not_announced_again(self):
        self.state["sent"].append(self.PACK)
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(bp, "announce_via_worker") as worker:
            self._notify(tg)
        worker.assert_not_called()
        tg.send_message.assert_not_called()
