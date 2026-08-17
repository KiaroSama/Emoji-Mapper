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

import build_pack as bp  # noqa: E402


class PublisherLock(unittest.TestCase):
    """Two publishers must not mutate one pack family at the same time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "state.json.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_holder_is_refused(self):
        with bp.exclusive_lock(self.lock):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock):
                    self.fail("a second publisher acquired the lock")

    def test_lock_is_released_on_exit(self):
        with bp.exclusive_lock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())

    def test_lock_is_released_even_on_error(self):
        with self.assertRaises(ZeroDivisionError):
            with bp.exclusive_lock(self.lock):
                1 / 0  # noqa: B018 - the point is to leave the block by raising
        self.assertFalse(self.lock.exists())

    def test_stale_lock_is_reclaimed(self):
        self.lock.write_text("pid=999 (crashed)", encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with bp.exclusive_lock(self.lock, stale_after=3600):
            pass          # must not raise

    def _make_stale(self) -> None:
        self.lock.write_text(json.dumps({"token": "gone", "pid": 999999999,
                                         "started": "2020-01-01 00:00:00 UTC"}),
                             encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))

    def test_a_reclaiming_run_never_deletes_a_fresh_claim(self):
        """Two runs judge the SAME dead record stale; only one may end up holding it.

        The window is between deciding "the holder is gone" and acting on that
        decision. An unconditional unlink there deletes whatever is on disk NOW,
        including the lock a faster run has just claimed -- so the recovery path
        itself hands the pack family to two live writers.

        The other run's claim is planted inside the liveness check, which is the
        last thing that happens before the old code would have unlinked.
        """
        self._make_stale()
        real_alive = bp._lock_owner_is_alive
        planted = {"done": False}

        def alive(pid):
            gone = real_alive(pid)              # the recorded pid really is gone
            if not planted["done"]:
                planted["done"] = True
                self.lock.write_text('{"token": "other", "pid": 1}',
                                     encoding="utf-8")
            return gone

        with mock.patch.object(bp, "_lock_owner_is_alive", alive):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=3600):
                    self.fail("took a lock another run was already holding")
        self.assertIn("other", self.lock.read_text(encoding="utf-8"),
                      "the other run's claim was deleted")

    def test_a_claim_overwritten_the_instant_it_lands_is_not_ours(self):
        """The last reclaim guard: read your own token back.

        The compare-and-delete and the O_EXCL loser check each close one
        ordering, but neither is atomic with respect to the OTHER process's
        whole sequence — a run can create the lock and have it replaced before
        it ever uses it. Reading the token back is what catches that, and a
        mutation test showed nothing covered it: deleting the check left the
        entire suite green.
        """
        self._make_stale()
        real_open, real_close = bp.os.open, bp.os.close
        state = {"opens": 0, "claim_fd": None}

        def counting_open(path, flags, *a, **kw):
            state["opens"] += 1
            fd = real_open(path, flags, *a, **kw)
            # Open 1 is the initial attempt (fails: the stale lock is there);
            # open 2 is the reclaim's successful create.
            if state["opens"] == 2 and Path(path) == self.lock:
                state["claim_fd"] = fd
            return fd

        def replace_once_our_claim_is_written(fd):
            real_close(fd)
            # Only now is our record on disk and the handle gone: the other run
            # replaces it before we ever look at it again.
            if fd == state["claim_fd"]:
                state["claim_fd"] = None
                self.lock.write_text('{"token": "someone-else", "pid": 1}',
                                     encoding="utf-8")

        with mock.patch.object(bp.os, "open", counting_open), \
                mock.patch.object(bp.os, "close", replace_once_our_claim_is_written):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=3600):
                    self.fail("proceeded holding a lock another run had taken")
        self.assertIn("someone-else", self.lock.read_text(encoding="utf-8"),
                      "the other run's record was clobbered on the way out")

    def test_the_loser_of_a_reclaim_race_gets_the_ordinary_busy_answer(self):
        """Both delete before either creates: O_EXCL decides, the loser backs off.

        The loser used to take an uncaught FileExistsError out of the reclaim's
        second _claim(), so callers that handle LockBusy saw a bare OSError from
        a path that is simply "someone else got there first".
        """
        self._make_stale()
        real_open = bp.os.open
        calls = {"n": 0}

        def racing_open(path, flags, *a, **kw):
            calls["n"] += 1
            # Call 1 is the opening claim (fails: the stale lock is still there).
            # Call 2 is the reclaim's claim -- the other run wins it by a hair.
            if calls["n"] == 2 and Path(path) == self.lock:
                self.lock.write_text('{"token": "other", "pid": 1}',
                                     encoding="utf-8")
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(bp.os, "open", racing_open):
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=3600):
                    self.fail("claimed a lock another run had just taken")


class LockOwnership(unittest.TestCase):
    """A lock may only be removed by the process that still owns it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "pack_x.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_live_holder_is_never_reclaimed_however_old(self):
        with bp.exclusive_lock(self.lock):
            old = time.time() - 10 * 24 * 3600
            os.utime(self.lock, (old, old))       # ancient, but WE are alive
            with self.assertRaises(bp.LockBusy):
                with bp.exclusive_lock(self.lock, stale_after=1):
                    self.fail("stole a lock from a live process")

    def test_a_dead_holder_is_reclaimed(self):
        self.lock.write_text(
            json.dumps({"token": "t", "pid": 999_999_999, "started": "old"}),
            encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))
        with bp.exclusive_lock(self.lock, stale_after=3600):
            pass                                   # must not raise

    def test_a_reclaimed_lock_is_not_deleted_by_the_old_holder(self):
        """The bug: the original holder unlinked the REPLACEMENT holder's lock."""
        cm = bp.exclusive_lock(self.lock)
        cm.__enter__()
        # Another process takes over the file entirely.
        self.lock.write_text(json.dumps(
            {"token": "other", "pid": 4242, "started": "now"}), encoding="utf-8")
        cm.__exit__(None, None, None)
        self.assertTrue(self.lock.exists(),
                        "must not remove a lock owned by someone else")

    def test_heartbeat_refreshes_the_lock(self):
        with bp.exclusive_lock(self.lock) as heartbeat:
            old = time.time() - 10_000
            os.utime(self.lock, (old, old))
            heartbeat()
            self.assertGreater(self.lock.stat().st_mtime, old + 1000)


class PackFamilyLock(unittest.TestCase):
    """Every tool touching one pack family must contend for the SAME lock."""

    def test_same_base_yields_the_same_path(self):
        self.assertEqual(bp.pack_family_lock_path("gvcryptoemoji"),
                         bp.pack_family_lock_path("gvcryptoemoji"))

    def test_different_bases_do_not_collide(self):
        self.assertNotEqual(bp.pack_family_lock_path("one"),
                            bp.pack_family_lock_path("two"))

    def test_unsafe_characters_are_normalised(self):
        p = bp.pack_family_lock_path("../../etc/passwd")
        self.assertEqual(p.parent, bp.LOCK_DIR)
        self.assertNotIn("..", p.name)


class PosixStaleLockReclaim(unittest.TestCase):
    """A crashed POSIX holder must not own its lock forever."""

    def test_no_such_process_is_reported_dead(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(bp._lock_owner_is_alive(4242))

    def test_permission_denied_means_it_exists(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=PermissionError):
            self.assertTrue(bp._lock_owner_is_alive(4242))

    def test_unknown_failure_stays_conservative(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", side_effect=OSError("weird")):
            self.assertTrue(bp._lock_owner_is_alive(4242))

    def test_a_running_process_is_alive(self):
        with mock.patch.object(bp.os, "name", "posix"), \
             mock.patch.object(bp.os, "kill", return_value=None):
            self.assertTrue(bp._lock_owner_is_alive(4242))


class CanonicalMapLock(unittest.TestCase):
    """Every writer of ticker_to_id.json must contend for one lock."""

    def test_all_callers_get_the_same_path(self):
        a, b = bp.canonical_map_lock(), bp.canonical_map_lock()
        self.assertEqual(a.args[0], b.args[0])

    def test_it_actually_excludes(self):
        with bp.canonical_map_lock():
            with self.assertRaises(bp.LockBusy):
                with bp.canonical_map_lock():
                    self.fail("two map writers held the lock at once")

    def test_it_is_not_the_pack_family_lock(self):
        with bp.canonical_map_lock():
            with bp.exclusive_lock(bp.pack_family_lock_path("gvcryptoemoji")):
                pass          # different concerns must not block each other


if __name__ == "__main__":
    unittest.main()
