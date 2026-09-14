"""build_collection at the CLI boundary: argument validation and main().

Split from test_build_collection_state.py, which covers the state machine
underneath. Both drive the same ``_CatalogFixture``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import build_collection as bc  # noqa: E402
from emojikit import collection_state as cs  # noqa: E402
from emojikit import telegram_api as tg_api  # noqa: E402
from emojikit.build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE)  # noqa: E402
from emojikit.packstate import (exclusive_lock)  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

from tests._bc_fixtures import (SET, SET2, FakeTG,  # noqa: E402
                                _CatalogFixture, _main, _make_png,
                                _sticker)


# --------------------------------------------------------------------------- #
# H-05 / M-01 / M-02 / M-03: CLI contracts
# --------------------------------------------------------------------------- #


class CliContract(_CatalogFixture):
    def _dry_run(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = _main("--base", "pk", "--title", "T", "--data-dir",
                       str(self.data), "--dry-run", *extra)
        return rc, out.getvalue()

    def test_second_publisher_fails_fast_while_the_lock_is_held(self):
        with exclusive_lock(cs._lock_path(self.data, "pk")):
            rc, _ = self._dry_run()
        self.assertEqual(rc, EXIT_FAILED)

    def test_the_lock_is_released_again(self):
        """Released means AVAILABLE, which is not the same as gone.

        The lock file is deliberately never unlinked: an unlinked inode is a
        lock nobody else can see, and reclaiming one by deleting it is how two
        publishers once ran at the same time. So the question is whether the
        next run can take it, and taking it is the only honest way to ask.
        """
        self.assertEqual(self._dry_run()[0], EXIT_OK)
        self.assertEqual(self._dry_run()[0], EXIT_OK)
        with exclusive_lock(cs._lock_path(self.data, "pk")):
            pass

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

    def create_emoji_set(self, user_id, name, title, path, fmt,
                         emojis, keywords, *, needs_repainting=False):
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

        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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

        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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
                raise tg_api.BotApiError(
                    "addStickerToSet failed: Bad Request: wrong file type")
            return original(*a, **kw)

        tg.add_emoji = refuse_the_first
        out = io.StringIO()
        with redirect_stdout(out):
            self._run(tg)
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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
            plan = cs.freeze_plan(cat, self.data, "pk", ["static"])
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
            # PARTIAL, not OK: nothing was validated, so a clean exit would
            # claim a check that never happened. Not FAILED either -- that is
            # the code a refusal returns, and this is the assertion below.
            self.assertEqual(self._run(tg, "--preflight"), EXIT_PARTIAL)
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
                raise tg_api.BotApiError(
                    "addStickerToSet failed: Bad Request: wrong file type")
            return original(*a, **kw)

        tg.add_emoji = refuse_the_first
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(state.get("sent", []), [],
                         "an incomplete pack must not be announced")

        # The NEXT run has nothing left to lose, so the link goes out then.
        with redirect_stdout(io.StringIO()):
            self._run(tg)
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual(len(state.get("sent", [])), 1,
                         "a clean run must still announce the pack")

    def test_a_clean_run_announces_normally(self):
        tg = FakeTG()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(tg), EXIT_OK)
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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
        state = json.loads(cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
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
            cs._state_path(self.data, "pk").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in state["sets"]], [SET])
        cs.load_state(self.data, "pk")              # a later run can still start

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
