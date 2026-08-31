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
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_collection as bc  # noqa: E402
import build_pack as bp  # noqa: E402
from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE,  # noqa: E402
                        LiveStateUnknown, exclusive_lock)
from emojikit import media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

from tests._bc_fixtures import FakeTG, _make_png, _sticker  # noqa: E402

SET = "pks1_by_GodVerifyEmojiMapperbot"
SET2 = "pks2_by_GodVerifyEmojiMapperbot"






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
        # 198 more catalogued items + the fixture's 2 = 200 emoji. With a logo
        # in the first slot of every set that is two sets, not one.
        #
        # REAL catalog rows, not synthetic plan keys: the dry run counts what
        # will actually publish (included, not already published, not skipped),
        # so a plan key with no catalog row is correctly counted as zero -- it
        # would not upload either.
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(198):
                img = self.data / "media" / "static" / f"x{i}.png"
                _make_png(img, color=(i % 200, 40, 60, 255))
                cat.add(content_key=f"s:x{i:030d}", fmt="static", file_path=img,
                        emojis=["😀"], keywords=[f"x{i}"])
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

    def test_new_set_starts_the_next_pack_while_this_one_is_half_empty(self):
        """--new-set is the ONLY supported way to leave a pack unfinished.

        Without it the next set opens only at `per_set`, so a deliberately
        half-empty pack could be continued only by shrinking --per-set (which
        caps every later set too) or by a second base (which re-uploads the
        whole catalog).
        """
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        self.assertEqual(len(tg.sets[SET]), 2)        # nowhere near per_set

        key2 = self._add_item(2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--new-set"), EXIT_OK)

        self.assertEqual(len(tg.sets[SET]), 2)        # left exactly as it was
        self.assertEqual(len(tg.sets[SET2]), 1)       # the new pack holds it
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.custom_emoji_id_for("pk", key2), f"{SET2}-0")
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual([s["index"] for s in state["sets"]], [1, 2])
        self.assertEqual(state["sets"][1]["keys"], [key2])

    def test_without_new_set_the_next_emoji_still_fills_the_current_pack(self):
        """The negative half: the flag must be what moves the emoji, not luck."""
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        self._add_item(2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        self.assertEqual(len(tg.sets[SET]), 3)        # appended, no new set
        self.assertNotIn(SET2, tg.sets)

    def _two_packs(self, tg):
        """Leave pack 1 half-empty and pack 2 open, so both have room."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)          # fills pack 1
        self._add_item(2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--new-set"), EXIT_OK)   # opens pack 2

    def test_into_pack_tops_up_an_older_pack_that_still_has_room(self):
        """The newest pack is not the only one that can be filled.

        Publishing always appended to `fmt_sets[-1]`, so a half-empty pack in
        the middle could never be topped up again once a later one existed.
        """
        tg = FakeTG()
        self._two_packs(tg)
        self.assertEqual(len(tg.sets[SET]), 2)
        self.assertEqual(len(tg.sets[SET2]), 1)

        key3 = self._add_item(3)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--into-pack", "1"), EXIT_OK)

        self.assertEqual(len(tg.sets[SET]), 3, "pack 1 should have taken it")
        self.assertEqual(len(tg.sets[SET2]), 1, "pack 2 must be untouched")
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual(cat.custom_emoji_id_for("pk", key3), f"{SET}-2")

    def test_the_record_written_is_the_pack_actually_uploaded_to(self):
        """The live count and key order must land on the TARGET's record.

        Both were written to `fmt_sets[-1]`, so filling a middle pack would have
        credited the upload to the LAST pack's record instead -- state
        describing a set the sticker never went into, which is precisely the
        drift `reconcile_set` exists to catch. A count check alone would pass.
        """
        tg = FakeTG()
        self._two_packs(tg)
        key3 = self._add_item(3)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--into-pack", "1"), EXIT_OK)

        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        pack1, pack2 = state["sets"][0], state["sets"][1]
        self.assertIn(key3, pack1["keys"], "the key belongs to pack 1's record")
        self.assertNotIn(key3, pack2["keys"], "pack 2's record must not claim it")
        self.assertEqual(pack1["live"], len(tg.sets[SET]))
        self.assertEqual(pack2["live"], len(tg.sets[SET2]))

    def test_into_pack_refuses_a_number_that_is_not_there(self):
        """Silence would fill some other pack and look like success."""
        tg = FakeTG()
        self._two_packs(tg)
        self._add_item(3)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--into-pack", "9"), EXIT_FAILED)
        self.assertEqual(len(tg.sets[SET]), 2)
        self.assertEqual(len(tg.sets[SET2]), 1)

    def test_into_pack_refuses_a_full_pack(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "2"), EXIT_OK)
        self._add_item(2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--per-set", "2",
                                       "--into-pack", "1"), EXIT_FAILED)

    def test_into_pack_and_new_set_together_are_rejected(self):
        """One opens a fresh pack, the other fills an old one."""
        tg = FakeTG()
        self._two_packs(tg)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg, "--new-set", "--into-pack", "1"),
                             EXIT_USAGE)

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
    def test_each_successful_upload_is_logged(self):
        """A healthy run used to write its plan and then nothing at all.

        Only FAILURES were logged, so an hour-long publish left a log file that
        stopped at "N pending" -- indistinguishable from a hung process while it
        ran, and useless afterwards for asking when a given emoji went in.
        """
        tg = FakeTG()
        out = io.StringIO()
        with redirect_stdout(out),                 self.assertLogs("build_collection", level="INFO") as caught:
            self.assertEqual(self._run(tg), EXIT_OK)
        uploads = [ln for ln in caught.output if "] uploaded " in ln]
        self.assertEqual(len(uploads), 2,
                         f"one line per sticker that landed; got {caught.output}")
        # The line has to say WHERE it went and how far along the run is, or it
        # answers none of the questions you open a log to answer.
        self.assertIn("1/2", uploads[0])
        self.assertIn("2/2", uploads[1])
        self.assertTrue(all("pk" in ln for ln in uploads),
                        "the set name belongs in the line")

    def test_a_file_telegram_will_never_accept_stops_being_retried(self):
        """"Will retry" on a permanent refusal means retrying forever.

        A real .tgs earned "Bad Request: wrong file type" because of a subtract
        mask. Nothing about that changes on a later run, yet every future publish
        re-attempted it and exited non-zero for it. A refusal aimed at the BYTES
        is recorded as a skip; a transient one still retries.
        """
        tg = FakeTG()
        original = tg.add_emoji
        calls = []

        def refuse_the_first(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise bp.BotApiError(
                    "addStickerToSet failed: Bad Request: wrong file type")
            return original(*a, **kw)

        tg.add_emoji = refuse_the_first
        out = io.StringIO()
        with redirect_stdout(out):
            self._run(tg)
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(len(state.get("skipped", [])), 1,
                         "the refused file must be recorded, not left pending")

        # A SECOND run must not touch it again.
        before = list(calls)
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        self.assertEqual(len(calls), len(before),
                         "a permanently refused file was retried on the next run")

    def test_the_dry_run_counts_what_will_publish_not_the_plan(self):
        """It reported 200 emoji and "2 sets" with one item deselected.

        The real answer was 199 + logo = exactly one set, and one-pack-or-two is
        the whole question a dry run is asked.

        Asserted on behaviour, not on the source of the dry-run branch: that
        text check broke the moment the filter was factored into
        ``pending_keys`` and shared with the publisher and the preflight, which
        is exactly the change that makes the three agree.
        """
        with Catalog(self.data / "catalog.db") as cat:
            keys = [it.content_key for it in cat.all_items()]
            cat.set_inclusion({keys[0]})            # deselect one
            plan = bc.freeze_plan(cat, self.data, "pk", ["static"])
            queued = bc.pending_keys(cat, plan, "static", "pk", set())
            self.assertNotIn(keys[0], queued, "a deselected item is not queued")
            self.assertEqual(len(queued), len(keys) - 1)

            # And an item already published to this base drops out too.
            cat.mark_uploaded(queued[0], "1", base="pk", set_name="pks1")
            self.assertNotIn(queued[0],
                             bc.pending_keys(cat, plan, "static", "pk", set()))

    def test_the_manifest_counts_the_pack_not_the_catalog_rows(self):
        """It said "199 emoji" for a 200-emoji pack.

        The brand logo is the set's first sticker but not a catalog item, so
        the key list is one short of what is in the pack. The manifest is a
        thing people open to see what a pack contains; reporting the internal
        row count there is reporting the wrong number.
        """
        with Catalog(self.data / "catalog.db") as cat:
            keys = [it.content_key for it in cat.all_items()]
            rec = {"name": "pks1_by_bot", "title": "Pack 1", "fmt": "static",
                   "keys": keys, "logo": True}
            bc.write_manifest(self.data, cat, rec, "pk")
            text = (self.data / "manifests" / "pks1_by_bot.md").read_text(
                encoding="utf-8")
        self.assertIn(f"{len(keys) + 1} emoji", text)
        self.assertNotIn(f"{len(keys)} emoji", text)
        self.assertIn("| 1 | brand logo |", text, "the logo is sticker 1")
        # ...and the catalog items start at 2, not at 1.
        self.assertIn("| 2 |", text)

    def test_a_set_without_a_logo_still_counts_plainly(self):
        with Catalog(self.data / "catalog.db") as cat:
            keys = [it.content_key for it in cat.all_items()]
            rec = {"name": "nologo_by_bot", "title": "No logo", "fmt": "static",
                   "keys": keys}
            bc.write_manifest(self.data, cat, rec, "pk")
            text = (self.data / "manifests" / "nologo_by_bot.md").read_text(
                encoding="utf-8")
        self.assertIn(f"{len(keys)} emoji", text)
        self.assertNotIn("brand logo", text)

    def test_preflight_refuses_early_and_publishes_nothing(self):
        """A file Telegram will not take must stop the run before it starts.

        The whole point: one `.tgs` with a subtract mask surfaced 46 minutes
        into a publish, after 99 uploads and two flood waits.
        """
        tg = FakeTG()
        tg.refuse = {p.name for p in
                     sorted((self.data / "media" / "static").glob("*"))[:1]}
        with redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(self._run(tg, "--preflight"), EXIT_OK)
        self.assertIn("REFUSED", out.getvalue())
        self.assertEqual(tg.uploaded, [], "preflight must publish nothing")
        self.assertFalse(tg.sets, "preflight must not create a set")

    def test_preflight_passes_a_clean_queue_and_still_uploads_nothing(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._run(tg, "--preflight"), EXIT_OK)
        self.assertEqual(len(tg.checked), 2, "every queued file is checked")
        self.assertIn("acceptable", out.getvalue())
        self.assertEqual(tg.uploaded, [])
        self.assertFalse(tg.sets)

    def test_a_transport_error_is_not_reported_as_a_bad_file(self):
        """"I could not ask" is not "Telegram said no".

        Reporting a dropped connection as a refusal would send someone editing
        artwork that was never the problem.
        """
        tg = FakeTG()

        def boom(user_id, path, fmt):
            tg.checked.append(path.name)
            raise RuntimeError("uploadStickerFile failed after 5 attempts")

        tg.check_uploadable = boom
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._run(tg, "--preflight"), EXIT_OK)
        self.assertNotIn("REFUSED", out.getvalue())

    def test_a_run_that_lost_an_emoji_does_not_announce_the_pack(self):
        """A channel link says "this pack is done". It must not lie.

        The end-of-run announcement was unconditional, so a run that finished
        199 of 200 -- one emoji refused by Telegram -- still posted the link.
        """
        tg = FakeTG()
        original = tg.add_emoji
        calls = []

        def refuse_the_first(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise bp.BotApiError(
                    "addStickerToSet failed: Bad Request: wrong file type")
            return original(*a, **kw)

        tg.add_emoji = refuse_the_first
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(state.get("sent", []), [],
                         "an incomplete pack must not be announced")

        # The NEXT run has nothing left to lose, so the link goes out then.
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(len(state.get("sent", [])), 1,
                         "a clean run must still announce the pack")

    def test_a_clean_run_announces_normally(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(len(state.get("sent", [])), 1)

    def test_a_transient_failure_is_still_retried(self):
        """The narrow rule must not swallow ordinary failures."""
        tg = FakeTG()
        original = tg.add_emoji
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("addStickerToSet failed after 5 attempts")
            return original(*a, **kw)

        tg.add_emoji = flaky
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        state = json.loads(bc._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(state.get("skipped", []), [],
                         "a transient failure must stay retryable")

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


if __name__ == "__main__":
    unittest.main()
