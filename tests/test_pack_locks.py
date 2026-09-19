"""The file locks that keep two publishers off one pack family.

Split out of `test_resume_safety` because these exercise a different mechanism
entirely: `exclusive_lock` and the lock-path helpers. Nothing here reads a
resume state file, fakes Telegram or needs an image -- the subject is purely
which process may hold which lock, and when a dead holder's claim may be taken.

`test_lock_order` is the sibling that checks the documented ORDER of these locks
by walking the AST; this one checks the mechanics of taking and releasing them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import build_pack as bp  # noqa: E402
from emojikit import packstate as ps  # noqa: E402



# Claimed by the `windows-safety` CI job: advisory locks are a native file-handle contract, not a POSIX one.
# tests/test_ci_coverage.py enforces the match both ways.
RUNS_ON_NATIVE_WINDOWS = True

class PublisherLock(unittest.TestCase):
    """Two publishers must not mutate one pack family at the same time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "state.json.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_holder_is_refused(self):
        with ps.exclusive_lock(self.lock):
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock):
                    self.fail("a second publisher acquired the lock")

    def test_lock_is_released_on_exit(self):
        """Released means the NEXT run can take it, which is the only property
        any caller depends on.

        This used to assert the file was deleted. Deleting it is now forbidden:
        the lock lives in the OS, attached to an open handle on this exact
        path, and unlinking would let a second process create a different file
        at the same name and hold a lock nobody else can see. The file staying
        put IS the fix, so the assertion moved to the behaviour.
        """
        with ps.exclusive_lock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertTrue(self.lock.exists(), "the lock target must be stable")
        with ps.exclusive_lock(self.lock):
            pass

    def test_lock_is_released_even_on_error(self):
        with self.assertRaises(ZeroDivisionError):
            with ps.exclusive_lock(self.lock):
                1 / 0  # noqa: B018 - the point is to leave the block by raising
        with ps.exclusive_lock(self.lock):
            pass

    def test_stale_lock_is_reclaimed(self):
        self.lock.write_text("pid=999 (crashed)", encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with ps.exclusive_lock(self.lock, stale_after=3600):
            pass          # must not raise

    def _make_stale(self) -> None:
        self.lock.write_text(json.dumps({"token": "gone", "pid": 999999999,
                                         "started": "2020-01-01 00:00:00 UTC"}),
                             encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))

    def test_a_record_rewritten_underneath_a_holder_grants_nothing(self):
        """The file's CONTENTS are a diagnostic, never a title deed.

        This replaces three tests that drove the old claim protocol through
        mocked `os.open`/`os.close` and planted records: compare-then-delete,
        the O_EXCL loser, and reading your own token back. That protocol is
        gone -- it was the defect. Every one of those guards was a
        compare-then-act on a file another process could change in between, and
        two real processes held one lock for 1.5s through the gap. The
        invariant they were all reaching for is asserted directly here, and
        proved across process boundaries in `test_pack_locks_exclusion.py`.
        """
        with ps.exclusive_lock(self.lock):
            # Someone scribbles a convincing claim over our record.
            self.lock.write_text(json.dumps(
                {"token": "someone-else", "pid": 1, "started": "now"}),
                encoding="utf-8")
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock):
                    self.fail("a rewritten record handed over ownership")

    def test_a_record_that_names_nobody_grants_nothing_either(self):
        """A holder that died between taking the lock and writing its record
        leaves an empty file. Empty must not read as free."""
        with ps.exclusive_lock(self.lock):
            self.lock.write_text("", encoding="utf-8")
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock):
                    self.fail("an empty record handed over ownership")

    def test_a_refusal_is_always_LockBusy_never_a_bare_oserror(self):
        """Callers catch LockBusy. A refusal arriving as a raw OSError escapes
        every one of them -- which is how the old reclaim path could abort a
        publish with an unhandled FileExistsError."""
        with ps.exclusive_lock(self.lock):
            with self.assertRaises(ps.LockBusy) as caught:
                with ps.exclusive_lock(self.lock):
                    pass
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertNotIsInstance(caught.exception, OSError)


class LockOwnership(unittest.TestCase):
    """A lock may only be removed by the process that still owns it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "pack_x.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_live_holder_is_never_reclaimed_however_old(self):
        with ps.exclusive_lock(self.lock):
            old = time.time() - 10 * 24 * 3600
            os.utime(self.lock, (old, old))       # ancient, but WE are alive
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock, stale_after=1):
                    self.fail("stole a lock from a live process")

    def test_a_dead_holder_is_reclaimed(self):
        self.lock.write_text(
            json.dumps({"token": "t", "pid": 999_999_999, "started": "old"}),
            encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with ps.exclusive_lock(self.lock, stale_after=3600):
            pass                                   # must not raise

    def test_a_killed_run_can_resume_within_minutes_not_hours(self):
        """The DEFAULT grace, with no stale_after passed -- what a user gets.

        A publish stopped halfway leaves a lock behind whose process is gone.
        The threshold used to be six hours, so resuming meant waiting or
        deleting a lock file by hand; the liveness check already refuses to
        touch a live holder, so age had nothing left to protect.
        """
        self.lock.write_text(
            json.dumps({"token": "t", "pid": 999_999_999, "started": "old"}),
            encoding="utf-8")
        old = time.time() - 300                    # five minutes ago
        os.utime(self.lock, (old, old))
        with ps.exclusive_lock(self.lock):         # no stale_after override
            pass                                   # must not raise

    def test_an_unwritten_record_never_reads_as_a_free_lock(self):
        """The old code claimed with O_CREAT|O_EXCL and wrote the record as a
        SECOND step, so for an instant the record was empty and parsed as
        "pid 0, i.e. dead". A timing grace existed only to paper over that gap.

        Taking the lock and owning it are now one OS call, so the gap is gone --
        but a leftover empty file must still never be mistaken for permission
        while somebody holds it.
        """
        self.lock.write_text("", encoding="utf-8")
        with ps.exclusive_lock(self.lock):          # nobody holds it: fine
            self.lock.write_text("", encoding="utf-8")
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock):
                    self.fail("stole a lock whose record said nothing")

    def test_a_reclaimed_lock_is_not_deleted_by_the_old_holder(self):
        """The bug: the original holder unlinked the REPLACEMENT holder's lock."""
        cm = ps.exclusive_lock(self.lock)
        cm.__enter__()
        # Another process takes over the file entirely.
        self.lock.write_text(json.dumps(
            {"token": "other", "pid": 4242, "started": "now"}), encoding="utf-8")
        cm.__exit__(None, None, None)
        self.assertTrue(self.lock.exists(),
                        "must not remove a lock owned by someone else")

    def test_heartbeat_refreshes_the_lock(self):
        with ps.exclusive_lock(self.lock) as heartbeat:
            old = time.time() - 10_000
            os.utime(self.lock, (old, old))
            heartbeat()
            self.assertGreater(self.lock.stat().st_mtime, old + 1000)


class PackFamilyLock(unittest.TestCase):
    """Every tool touching one pack family must contend for the SAME lock."""

    def test_same_base_yields_the_same_path(self):
        self.assertEqual(ps.pack_family_lock_path("cryptoemoji"),
                         ps.pack_family_lock_path("cryptoemoji"))

    def test_different_bases_do_not_collide(self):
        self.assertNotEqual(ps.pack_family_lock_path("one"),
                            ps.pack_family_lock_path("two"))

    def test_unsafe_characters_are_normalised(self):
        p = ps.pack_family_lock_path("../../etc/passwd")
        self.assertEqual(p.parent, ps.LOCK_DIR)
        self.assertNotIn("..", p.name)


class PosixStaleLockReclaim(unittest.TestCase):
    """A crashed POSIX holder must not own its lock forever."""

    def test_no_such_process_is_reported_dead(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(ps._lock_owner_is_alive(4242))

    def test_permission_denied_means_it_exists(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=PermissionError):
            self.assertTrue(ps._lock_owner_is_alive(4242))

    def test_unknown_failure_stays_conservative(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=OSError("weird")):
            self.assertTrue(ps._lock_owner_is_alive(4242))

    def test_a_running_process_is_alive(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", return_value=None):
            self.assertTrue(ps._lock_owner_is_alive(4242))


class CanonicalMapLock(unittest.TestCase):
    """Every writer of ticker_to_id.json must contend for one lock."""

    def test_all_callers_get_the_same_path(self):
        a, b = ps.canonical_map_lock(), ps.canonical_map_lock()
        self.assertEqual(a.args[0], b.args[0])

    def test_it_actually_excludes(self):
        with ps.canonical_map_lock():
            with self.assertRaises(ps.LockBusy):
                with ps.canonical_map_lock():
                    self.fail("two map writers held the lock at once")

    def test_it_is_not_the_pack_family_lock(self):
        with ps.canonical_map_lock():
            with ps.exclusive_lock(ps.pack_family_lock_path("cryptoemoji")):
                pass          # different concerns must not block each other


if __name__ == "__main__":
    unittest.main()
