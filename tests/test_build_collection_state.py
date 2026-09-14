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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_collection as bc  # noqa: E402
from emojikit import collection_state as cs  # noqa: E402
from emojikit import collection_reconcile as cr  # noqa: E402
from build_pack import (EXIT_FAILED, EXIT_OK)  # noqa: E402
from emojikit.telegram_api import (LiveStateUnknown)  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

from tests._bc_fixtures import (SET, SET2, DownloadingTG, FakeTG,  # noqa: E402
                                _CatalogFixture, _main, _sticker)


# --------------------------------------------------------------------------- #
# C-10 / H-01: state and plan files fail closed, and are written atomically
# --------------------------------------------------------------------------- #
class StateFileContract(_CatalogFixture):
    def test_absent_file_uses_the_default(self):
        self.assertEqual(cs.load_json(self.data / "nope.json", {"d": 1}), {"d": 1})

    def test_truncated_state_refuses_to_start_from_scratch(self):
        path = cs._state_path(self.data, "pk")
        path.write_text('{"base": "pk", "sets": [{"name": "pks1', encoding="utf-8")
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_truncated_plan_refuses_to_refreeze(self):
        cs._plan_path(self.data, "pk").write_text('{"static": ["s:a"',
                                                  encoding="utf-8")
        with self.assertRaises(cs.StateError):
            cs.load_plan(self.data, "pk")

    def test_plan_of_the_wrong_shape_is_rejected(self):
        cs._plan_path(self.data, "pk").write_text('{"static": "s:a"}',
                                                  encoding="utf-8")
        with self.assertRaises(cs.StateError):
            cs.load_plan(self.data, "pk")

    def test_state_of_another_pack_family_is_rejected(self):
        cs.save_json(cs._state_path(self.data, "pk"),
                     {"base": "other", "sets": [], "sent": []})
        with self.assertRaises(cs.StateError) as ctx:
            cs.load_state(self.data, "pk")
        self.assertIn("other", str(ctx.exception))

    def test_state_schema_is_checked(self):
        cs.save_json(cs._state_path(self.data, "pk"),
                     {"base": "pk", "sets": [{"name": SET, "fmt": "nope",
                                              "index": 1}], "sent": []})
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_a_failed_write_leaves_the_previous_state_intact(self):
        path = cs._state_path(self.data, "pk")
        cs.save_json(path, {"base": "pk", "sets": [], "sent": ["first"]})
        # Simulate the process dying at the very end of the write: with a
        # non-atomic write_text the destination is already truncated by then.
        with mock.patch("build_pack.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                cs.save_json(path, {"base": "pk", "sets": [], "sent": ["second"]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sent"],
                         ["first"])

    # ----- M-11: the WHOLE shape is checked, before anything mutates ------- #
    def _bad_state(self, *sets, **top) -> None:
        state = {"base": "pk", "sets": list(sets), "sent": []}
        state.update(top)
        cs.save_json(cs._state_path(self.data, "pk"), state)

    def _set(self, **over) -> dict:
        s = {"fmt": "static", "index": 1, "name": SET, "title": "Pack 1",
             "live": 2, "logo": False, "keys": list(self.keys)}
        s.update(over)
        return s

    def test_a_wholly_consistent_state_is_accepted(self):
        self._bad_state(self._set())
        self.assertEqual(len(cs.load_state(self.data, "pk")["sets"]), 1)

    def test_the_same_index_in_two_formats_is_fine(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(fmt="video", name="pkv1_by_bot", live=1,
                                  keys=[self.keys[1]]))
        self.assertEqual(len(cs.load_state(self.data, "pk")["sets"]), 2)

    def test_a_negative_live_count_is_rejected(self):
        self._bad_state(self._set(live=-1, keys=[]))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_a_live_count_above_telegrams_cap_is_rejected(self):
        self._bad_state(self._set(live=201, keys=[]))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_more_recorded_keys_than_live_stickers_is_rejected(self):
        # The cid mapping reads live[i + offset] for every key, so this state
        # would index past the end -- or worse, onto somebody else's sticker.
        self._bad_state(self._set(live=1))
        with self.assertRaises(cs.StateError) as ctx:
            cs.load_state(self.data, "pk")
        self.assertIn("records 2", str(ctx.exception))

    def test_the_logo_slot_counts_towards_that_bound(self):
        self._bad_state(self._set(live=2, logo=True))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_one_emoji_recorded_twice_in_a_set_is_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0], self.keys[0]]))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_one_emoji_recorded_in_two_sets_is_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(index=2, name=SET2, live=1,
                                  keys=[self.keys[0]]))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_two_sets_sharing_a_name_are_rejected(self):
        self._bad_state(self._set(keys=[self.keys[0]], live=1),
                        self._set(index=2, keys=[self.keys[1]], live=1))
        with self.assertRaises(cs.StateError):
            cs.load_state(self.data, "pk")

    def test_a_repeated_or_backwards_index_is_rejected(self):
        for second in ({"index": 1}, {"index": 0}):
            self._bad_state(self._set(index=2, keys=[self.keys[0]], live=1),
                            self._set(name=SET2, keys=[self.keys[1]], live=1,
                                      **second))
            with self.assertRaises(cs.StateError):
                cs.load_state(self.data, "pk")

    def test_malformed_field_types_are_rejected(self):
        for over in ({"keys": "not-a-list"}, {"keys": [1, 2]}, {"logo": "yes"},
                     {"live": "two"}, {"live": True}, {"index": True},
                     {"title": ""}, {"name": ""}):
            self._bad_state(self._set(**over))
            with self.assertRaises(cs.StateError):
                cs.load_state(self.data, "pk")

    def test_malformed_sent_and_skipped_entries_are_rejected(self):
        for top in ({"sent": [None]}, {"sent": [""]}, {"skipped": [{"k": 1}]}):
            self._bad_state(**top)
            with self.assertRaises(cs.StateError):
                cs.load_state(self.data, "pk")

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
        self.assertFalse(cs._plan_path(self.data, "pk").exists())

    def test_main_stops_on_an_unreadable_plan(self):
        cs._plan_path(self.data, "pk").write_text("{oops", encoding="utf-8")
        self.assertEqual(_main("--base", "pk", "--title", "T",
                               "--data-dir", str(self.data), "--dry-run"),
                         EXIT_FAILED)


# --------------------------------------------------------------------------- #
# H-02 / H-03 / H-04: live-set identity
# --------------------------------------------------------------------------- #
class LiveSetDrift(_CatalogFixture):
    def _reconcile(self, tg, s):
        with Catalog(self.data / "catalog.db") as cat:
            return cr.reconcile_set(tg, cat, s, self.data, "pk")

    def test_unknown_live_state_aborts_instead_of_guessing(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                               _sticker("UP-item1", "c1")]}, unknown=[SET])
        s = self._state_set()
        with self.assertRaises(LiveStateUnknown):
            self._reconcile(tg, s)
        self.assertEqual(s["live"], 2)          # untouched: nothing was guessed

    def test_deleted_set_is_reported_not_treated_as_the_recorded_count(self):
        tg = FakeTG(sets={})                    # owner deleted the whole pack
        with self.assertRaises(cs.SetDrift) as ctx:
            self._reconcile(tg, self._state_set())
        self.assertIn("no longer exists", str(ctx.exception))

    def test_shrunk_set_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0")]})
        with self.assertRaises(cs.SetDrift):
            self._reconcile(tg, self._state_set())

    def test_reordered_set_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item1", "c1"),
                                _sticker("UP-item0", "c0")]})
        with self.assertRaises(cs.SetDrift) as ctx:
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
        with self.assertRaises(cs.SetDrift):
            self._reconcile(tg, s)
        self.assertEqual(s["keys"], [self.keys[0]])   # nothing mis-attributed

    def test_the_same_emoji_live_twice_is_drift(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1"),
                                _sticker("UP-item0", "c2")]})
        with self.assertRaises(cs.SetDrift) as ctx:
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
        with self.assertRaises(cs.SetDrift):
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
            with self.assertRaises(cs.SetDrift):
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
            with self.assertRaises(cs.SetDrift) as ctx:
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
            with self.assertRaises(cs.SetDrift):
                cr.reconcile_set(tg, cat, s, self.data, "pk")

    def test_content_resolution_rescues_a_re_uploaded_identical_picture(self):
        # A new id is not automatically a different emoji: if the bytes still
        # resolve to the recorded key it is the same picture, not drift. A
        # re-upload is a NEW sticker, so its custom_emoji_id is new too and only
        # the content can settle it.
        self._read_back(*self.keys)
        tg = DownloadingTG(sets={SET: [_sticker("RE-UPLOADED", "c9"),
                                       _sticker("UP-item1", "c1")]})
        with mock.patch.object(cr.identity, "content_key",
                               lambda p, fmt: self.keys[0]), \
                mock.patch.object(cr.media, "telegram_sticker_format",
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
            with self.assertRaises(cs.SetDrift):
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
            with self.assertRaises(cs.SetDrift):
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
            self.assertEqual(cr.reconcile_set(tg, cat, s, self.data, "pk"), 2)
        self.assertEqual(s["keys"], [self.keys[0]])   # the tail stayed unowned
        self.assertFalse(cr._set_is_open(s))

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
            with self.assertRaises(cs.SetDrift) as ctx:
                cr.reconcile_set(tg, cat, s, self.data, "pk")
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
            with self.assertRaises(cs.SetDrift) as ctx:
                cr.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertIn("could not be examined", str(ctx.exception))
        self.assertEqual(s["keys"], [self.keys[0]])

    def test_a_fully_attributed_set_stays_open(self):
        tg = FakeTG(sets={SET: [_sticker("UP-item0", "c0"),
                                _sticker("UP-item1", "c1")]})
        s = self._state_set(keys=[self.keys[0]], live=1)
        with Catalog(self.data / "catalog.db") as cat:
            cr.reconcile_set(tg, cat, s, self.data, "pk")
        self.assertTrue(cr._set_is_open(s))


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
            with self.assertRaises(cs.SetDrift) as ctx:
                cr.reconcile_set(tg, cat, s, self.data, "pk")
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
            with self.assertRaises(cs.SetDrift):
                cr.reconcile_set(tg, cat, s, self.data, "pk")
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
            self.assertEqual(cr.reconcile_set(tg, cat, s, self.data, "pk"), 3)
        self.assertEqual(s["keys"], self.keys)
        self.assertFalse(cr._set_is_open(s))
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
