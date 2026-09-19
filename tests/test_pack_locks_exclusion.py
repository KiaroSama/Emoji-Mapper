"""Two processes must never hold one pack-family lock at the same time.

F08. The lock used to decide ownership by reading a file, comparing a token and
unlinking -- and its own recovery path could seat two publishers at once:

    A reads the dead holder's record and judges it stale
    A pauses, about to unlink
    B reads the SAME record, unlinks, claims, verifies its token, ENTERS
    A resumes, unlinks B's LIVE claim, claims, verifies its token, ENTERS

Two real processes reproduced it holding the same lock for 1.5 seconds. Every
guard in that protocol was a compare-then-act on a file another process can
change in between, so no further check could have closed it; the lock is an OS
lock now, held for the whole critical section.

These are real subprocesses on purpose. The defect is about what two operating
system processes can do to one file, and an in-process fake cannot fail the way
the bug failed. Each child journals the interval it believed it owned the lock,
and the assertions are about OVERLAP -- the only thing that actually matters.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import packstate as ps  # noqa: E402


# Claimed by the `windows-safety` CI job: writer exclusion depends on native handle semantics.
# tests/test_ci_coverage.py enforces the match both ways.
RUNS_ON_NATIVE_WINDOWS = True

PY = sys.executable

# Long enough that two overlapping owners cannot miss each other, short enough
# that the whole module stays a couple of seconds.
HOLD = 0.35
CHILD_TIMEOUT = 60

# One child program, parameterised by environment. It journals `enter`/`exit`
# around the critical section so the parent can compare intervals, and reports
# `busy` when the lock was correctly refused.
CHILD = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["REPO"])
from emojikit import packstate as ps

LOCK = os.environ["LOCK"]
JOURNAL = os.environ["JOURNAL"]
HOLD = float(os.environ["HOLD"])
START_AT = float(os.environ.get("START_AT", "0"))
CRASH = os.environ.get("CRASH") == "1"

def note(what):
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"what": what, "pid": os.getpid(), "t": time.time()}) + "\n")

# A shared wall-clock start makes every child contend in the same instant
# instead of in launch order, which is what a race needs.
while START_AT and time.time() < START_AT:
    time.sleep(0.002)

try:
    with ps.exclusive_lock(LOCK):
        note("enter")
        if CRASH:
            # Die holding it, with no unwinding at all: the OS must be what
            # releases the lock, not our finally block.
            os._exit(9)
        time.sleep(HOLD)
        note("exit")
except ps.LockBusy:
    note("busy")
    sys.exit(3)
'''


class _LockHarness(unittest.TestCase):
    """Temp lock file, a child program on disk, and interval bookkeeping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.lock = self.dir / "pack_family.lock"
        self.journal = self.dir / "journal.jsonl"
        self.journal.write_text("", encoding="utf-8")
        self.child = self.dir / "child.py"
        self.child.write_text(CHILD, encoding="utf-8")
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        # Every child must be gone BEFORE the directory is removed: Windows
        # refuses to delete a file another process still has open, and the lock
        # file is deliberately never unlinked.
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=CHILD_TIMEOUT)
        self.tmp.cleanup()

    def spawn(self, *, crash=False, start_at=0.0, hold=HOLD):
        env = dict(os.environ, REPO=str(ROOT), LOCK=str(self.lock),
                   JOURNAL=str(self.journal), HOLD=str(hold),
                   START_AT=str(start_at), PYTHONUTF8="1")
        if crash:
            env["CRASH"] = "1"
        p = subprocess.Popen([PY, str(self.child)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        self.procs.append(p)
        return p

    def wait_all(self):
        for p in self.procs:
            p.communicate(timeout=CHILD_TIMEOUT)

    def intervals(self):
        """[(pid, enter, exit)] for every child that believed it held the lock."""
        events = [json.loads(line) for line in
                  self.journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        spans = {}
        for e in events:
            spans.setdefault(e["pid"], {})[e["what"]] = e["t"]
        return [(pid, v["enter"], v.get("exit", float("inf")))
                for pid, v in spans.items() if "enter" in v]

    def assertNeverTwoOwners(self, spans):
        """The whole point. Overlap is the failure, not the count of entries."""
        ordered = sorted(spans, key=lambda s: s[1])
        for (pid_a, a0, a1), (pid_b, b0, b1) in zip(ordered, ordered[1:], strict=False):
            overlap = min(a1, b1) - max(a0, b0)
            self.assertLessEqual(
                overlap, 0,
                f"pids {pid_a} and {pid_b} both held the lock for {overlap:.3f}s")

    def make_stale_leftover(self, pid=999_999):
        """A lock file left behind by a run that is provably gone.

        This is the state the old recovery path existed for, and the state its
        two-owner window opened in.
        """
        self.lock.write_text(
            json.dumps({"token": "gone:0", "pid": pid,
                        "started": "2020-01-01 00:00:00 UTC"}), encoding="utf-8")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))


class OnlyOneProcessHoldsTheLock(_LockHarness):
    def test_two_racing_runs_over_a_stale_leftover_never_overlap(self):
        """The audit's scenario. A leftover record from a dead pid is exactly
        what used to send both runs down the reclaim path together."""
        self.make_stale_leftover()
        start = time.time() + 0.4
        for _ in range(2):
            self.spawn(start_at=start)
        self.wait_all()
        spans = self.intervals()
        self.assertGreaterEqual(len(spans), 1, "nobody acquired a free lock")
        self.assertNeverTwoOwners(spans)

    def test_four_runs_starting_at_once_never_overlap(self):
        """Concurrent startup, no leftover file at all: the create path has to
        be exclusive too, not only the recovery path."""
        start = time.time() + 0.5
        for _ in range(4):
            self.spawn(start_at=start)
        self.wait_all()
        spans = self.intervals()
        self.assertGreaterEqual(len(spans), 1)
        self.assertNeverTwoOwners(spans)

    def test_a_live_holder_is_never_displaced_however_old_the_record_looks(self):
        """Age must not condemn a lock. The record is backdated far past any
        staleness window while its holder is genuinely running."""
        holder = self.spawn(hold=2.5)
        deadline = time.time() + 20
        while not self.intervals() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.intervals(), "the first child never acquired")
        old = time.time() - 10_000
        os.utime(self.lock, (old, old))          # look ancient to anyone asking

        with self.assertRaises(ps.LockBusy):
            with ps.exclusive_lock(self.lock, stale_after=1):
                self.fail("stole a lock from a live holder")
        holder.communicate(timeout=CHILD_TIMEOUT)
        self.assertNeverTwoOwners(self.intervals())


class TheOperatingSystemReleasesTheLock(_LockHarness):
    def test_a_crashed_holder_leaves_an_acquirable_lock(self):
        """`os._exit` runs no finally block, so nothing in this codebase gets
        the chance to release. Recovery has to come from the OS closing the
        handle -- which is what removes the need for a staleness rule at all."""
        crashed = self.spawn(crash=True)
        crashed.communicate(timeout=CHILD_TIMEOUT)
        self.assertEqual(crashed.returncode, 9)
        self.assertTrue(self.lock.exists(), "the crashed run's file should remain")

        with ps.exclusive_lock(self.lock):
            pass          # must not raise: the OS dropped the dead handle

    def test_an_exception_inside_the_block_still_releases(self):
        with self.assertRaises(ZeroDivisionError):
            with ps.exclusive_lock(self.lock):
                1 / 0  # noqa: B018 - leaving the block by raising is the point
        with ps.exclusive_lock(self.lock):
            pass

    def test_releasing_lets_the_next_run_in(self):
        """Release is proved by a successful re-acquire, not by the file being
        gone. The file deliberately stays: deleting it is what would let two
        processes hold locks on two different inodes at one path."""
        with ps.exclusive_lock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertTrue(self.lock.exists(), "the lock target must be stable")
        with ps.exclusive_lock(self.lock):
            pass

    def test_the_lock_file_is_never_recreated_underneath_a_holder(self):
        """The unlinked-inode loophole, asserted directly: a full acquire and
        release must leave the SAME file, not a fresh one."""
        with ps.exclusive_lock(self.lock):
            first = self.lock.stat()
        with ps.exclusive_lock(self.lock):
            second = self.lock.stat()
        self.assertEqual((first.st_ino, first.st_dev), (second.st_ino, second.st_dev),
                         "the lock target was replaced, not reused")


class RefusalIsInformative(_LockHarness):
    def test_a_second_acquisition_in_one_process_is_refused(self):
        """Non-reentrancy is load-bearing: the documented project-wide lock
        ORDER only means anything if taking the same lock twice cannot pass."""
        with ps.exclusive_lock(self.lock):
            with self.assertRaises(ps.LockBusy):
                with ps.exclusive_lock(self.lock):
                    self.fail("re-entered a lock this process already held")

    def test_the_refusal_names_the_live_holder(self):
        holder = self.spawn(hold=2.5)
        deadline = time.time() + 20
        while not self.intervals() and time.time() < deadline:
            time.sleep(0.02)
        pid = self.intervals()[0][0]
        with self.assertRaises(ps.LockBusy) as caught:
            with ps.exclusive_lock(self.lock):
                pass
        holder.communicate(timeout=CHILD_TIMEOUT)
        self.assertIn(str(pid), str(caught.exception))
        self.assertIn("Refusing to mutate", str(caught.exception))

    def test_an_unreadable_record_still_refuses_rather_than_proceeds(self):
        """The record is a hint for the message, never the authority. Garbage
        in it must not become permission to enter."""
        holder = self.spawn(hold=2.0)
        deadline = time.time() + 20
        while not self.intervals() and time.time() < deadline:
            time.sleep(0.02)
        self.lock.write_text("not json at all", encoding="utf-8")
        with self.assertRaises(ps.LockBusy):
            with ps.exclusive_lock(self.lock):
                self.fail("a corrupt record granted ownership")
        holder.communicate(timeout=CHILD_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
