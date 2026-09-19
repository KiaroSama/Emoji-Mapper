"""Bound SQLite backup's BUSY/LOCKED retries without losing WAL consistency."""
from __future__ import annotations

import math
import sqlite3
import time

BACKUP_TIMEOUT = 30.0
# Rollback writes onto the LIVE catalog, and an interrupted online backup leaves
# its destination incomplete. Waiting out a competing reader is recoverable;
# abandoning the restore half-written is what the bundle exists to prevent, so
# this path trades a long wait for never aborting mid-write at the 30 s mark.
# Still bounded: an indefinite hang is what P06 set out to remove.
RESTORE_TIMEOUT = 900.0


class BackupTimeout(RuntimeError):
    """A consistent snapshot could not finish within its retry budget."""


def backup(source: sqlite3.Connection, destination: sqlite3.Connection, *,
           timeout: float | None = None) -> None:
    """Copy with bounded steps and lock waits; restore connection settings.

    sqlite3.connect(timeout=...) does not bound Connection.backup(), which
    repeatedly sleeps on BUSY/LOCKED. The progress callback covers retries and
    incremental work. A completed copy is accepted; other failures propagate.
    This bounds SQLite retries, not an uninterruptible operating-system I/O.
    """
    budget = BACKUP_TIMEOUT if timeout is None else timeout
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("backup timeout must be finite and positive")
    deadline = time.monotonic() + budget
    original = [(con, con.execute("PRAGMA busy_timeout").fetchone()[0])
                for con in (source, destination)]

    def progress(status: int, remaining: int, total: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise BackupTimeout(f"SQLite snapshot did not finish within {budget:g}s")

    try:
        for con, _ in original:
            con.execute("PRAGMA busy_timeout=25")
        source.backup(destination, pages=128, progress=progress, sleep=min(0.025, budget))
    finally:
        for con, milliseconds in original:
            con.execute(f"PRAGMA busy_timeout={milliseconds}")
