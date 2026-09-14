"""One native lock for all permanent writers in a canonical catalog directory.

Order: catalog ownership, then existing family locks, then map locks. A writer
may call another writer in the same thread; maintenance never enters a writer.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from emojikit.packstate import LockBusy, exclusive_lock

JOURNAL_NAME = "identity-migration.journal.json"
_owners = threading.local()


def canonical_directory(data_dir: Path) -> Path:
    return Path(os.path.normcase(str(Path(data_dir).resolve())))


def lock_path(data_dir: Path) -> Path:
    return canonical_directory(data_dir) / ".maintenance.lock"


@contextmanager
def _ownership(data_dir: Path, mode: str):
    directory = canonical_directory(data_dir)
    key = (os.getpid(), str(directory))
    held = getattr(_owners, "held", None)
    if held is None:
        held = _owners.held = {}
    active = held.get(key)
    if active is not None and active[0] != mode:
        raise LockBusy(f"catalog {directory} already has {active[0]} ownership")
    if active is None:
        native = exclusive_lock(lock_path(directory))
        native.__enter__()
        active = held[key] = [mode, 0, native]
    active[1] += 1
    try:
        if mode == "writer" and (directory / JOURNAL_NAME).exists():
            raise LockBusy(
                f"catalog {directory} has an interrupted identity migration; "
                "resume migrate-video-keys --apply or restore its rollback bundle "
                "before writing")
        yield directory
    finally:
        active[1] -= 1
        if not active[1]:
            del held[key]
            active[2].__exit__(None, None, None)


def writer(data_dir: Path):
    """Protect discovery, permanent media, database and state as one operation."""
    return _ownership(data_dir, "writer")


def maintenance(data_dir: Path):
    """Own migration/recovery, including its survey and final verification."""
    return _ownership(data_dir, "maintenance")
