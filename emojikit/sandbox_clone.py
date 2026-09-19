"""Make a sandbox catalog that shares nothing with the owner's real one.

`scripts/panel_sandbox.py` is the entry point; this module is the part with
logic worth testing, so it lives where a test can import it without importing a
CLI -- and where the file-size rule wants it, named for the responsibility it
carries rather than doubling the wrapper.

What it owns: cloning a catalog into a throwaway directory, the marker that says
who a sandbox directory belongs to, and the sweep that reclaims abandoned ones.

Almost nothing here is new machinery. Opening a ``Catalog`` on the SOURCE is
already the writer lease AND already refuses an interrupted migration;
``sqlite_snapshot.backup`` is already the bounded WAL-inclusive snapshot;
``state_artifacts.state_files`` is already the shared inventory that migration
and rollback use. Reaching for those instead of writing four new versions is the
point -- a second inventory is how `pack_plan.json` came to be missing from the
clone in the first place.

Terms, because three documents used three words: a LOCK is the operating-system
lock (`exclusive_lock`); a LEASE is `writer()`, which takes that lock and also
refuses when a migration journal is present.

This is cooperative isolation between the owner's own tools, not an OS boundary
against hostile code running as the same user. A process that means to reach
`collection/` still can. What it stops is reaching it by ACCIDENT, which is how
the damage actually happened: an agent's synthetic drags rewrote an afternoon of
the owner's manual ordering.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
from pathlib import Path

from emojikit import sqlite_snapshot
from emojikit.catalog import Catalog
from emojikit.maintenance import lock_path
from emojikit.packstate import exclusive_lock
from emojikit.state_artifacts import state_files

# One definition, imported by the wrapper: the sweep decides what counts as a
# sandbox by this prefix, so two spellings would mean the sweep either misses
# real sandboxes or claims directories that are not ours.
TMP_PREFIX = "panel-sandbox-"
MARKER_NAME = ".sandbox-owner.json"
MARKER_VERSION = 1


class CloneRefused(RuntimeError):
    """The clone cannot be made faithfully, so it is not made at all."""


def safe_key(content_key: str) -> str:
    """A content key as a filename.

    `content_key()` returns `s:<32 hex>` and `collision_key()` returns
    `s:<hex>:<hex>`. On NTFS a colon in a path opens an ALTERNATE DATA STREAM
    rather than a file, so `media/s:abc.png` is not the file it looks like --
    and this project is Windows-first. `_` is an unambiguous replacement: a key
    is only a prefix letter, colons and hex, so no two keys can collide here.
    """
    return content_key.replace(":", "_")


# --------------------------------------------------------------------------- #
# Ownership marker
# --------------------------------------------------------------------------- #

def write_marker(directory: Path) -> Path:
    """Record that this sandbox directory is ours."""
    directory = Path(directory).resolve()
    path = directory / MARKER_NAME
    path.write_text(json.dumps({
        "version": MARKER_VERSION,
        # The marker is DIRECTORY-BOUND: a copy that ends up somewhere else
        # describes a path it no longer sits in, and is therefore foreign.
        "directory": str(directory),
        "created_utc": _dt.datetime.now(_dt.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Diagnostic only. PIDs are recycled, so this decides nothing; liveness
        # is proved by taking the lock, never by reading this number.
        "pid": os.getpid(),
    }, indent=2), encoding="utf-8")
    return path


def marker_is_self_describing(directory: Path) -> bool:
    """True only when a valid marker names the directory holding it."""
    directory = Path(directory)
    try:
        if directory.is_symlink() or not directory.is_dir():
            return False
        doc = json.loads((directory / MARKER_NAME).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("version") != MARKER_VERSION:
            return False
        named = doc.get("directory")
        return isinstance(named, str) and Path(named) == directory.resolve()
    except (OSError, ValueError, TypeError):
        # Unreadable, absent, malformed: all mean "cannot tell", and cannot tell
        # is not abandoned. Every failure here refuses to delete.
        return False


def sweep_stale(root: Path | None = None) -> int:
    """Remove sandbox directories whose server is provably gone.

    ``atexit`` does not run when a process is killed, and this server is
    normally ended by killing it, so cleaning at START is the only cleanup that
    survives the way the thing is actually stopped.

    Reclaiming needs BOTH halves. The marker says the directory is one of ours
    and has not been moved; taking its lock says nobody is still serving it.
    Either alone is a guess: the previous version matched the name prefix and
    called ``rmtree`` on everything, so starting a second sandbox deleted the
    catalog the first one was serving.

    Acquisition is the liveness test precisely because ``exclusive_lock`` is
    non-blocking -- a live sandbox raises ``LockBusy`` and is skipped instead of
    hanging the caller.
    """
    import tempfile

    base = Path(tempfile.gettempdir()) if root is None else Path(root)
    removed = 0
    for old in sorted(base.glob(f"{TMP_PREFIX}*")):
        # Checked before anything inside is read, and never followed.
        if old.is_symlink() or not old.is_dir():
            continue
        if not marker_is_self_describing(old):
            continue
        try:
            with exclusive_lock(lock_path(old)):
                _empty_except_lock(old, lock_path(old))
                removed += 1
        except Exception:  # noqa: BLE001 - a busy or unreadable clone is simply left
            continue
    return removed


def _empty_except_lock(directory: Path, keep: Path) -> None:
    """Delete a reclaimed directory's contents, keeping the lock file.

    The lock file is deliberately left behind -- and so is the directory that
    holds it. On POSIX, unlinking the inode a holder locked lets two processes
    each hold a lock at the same path and each believe it is alone; that is the
    hole `exclusive_lock`'s own docstring records, and a tombstone costs nothing.
    """
    keep = keep.resolve()
    for entry in directory.iterdir():
        if entry.resolve() == keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Cloning
# --------------------------------------------------------------------------- #

def _check_destination(source: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        raise CloneRefused(f"destination {dest} already exists; refusing to erase it")
    if source == dest or source in dest.parents or dest in source.parents:
        raise CloneRefused(f"destination {dest} overlaps the source {source}")


def _resolve_media(raw: str, source: Path, project_root: Path) -> Path:
    """Where a stored path actually points.

    Three shapes reach here and all three have to be copied: inside the source,
    an absolute path elsewhere (the owner's archive, which a published pack's
    media is MOVED to), and a project-relative path. The previous version gave
    up on anything it could not make relative to the source, which left those
    rows pointing at the owner's real archive from inside the sandbox.
    """
    path = Path(raw)
    if path.is_absolute():
        return path
    for base in (source, source.parent, project_root):
        candidate = base / path
        if candidate.exists():
            return candidate
    return project_root / path


def clone_catalog(source: Path, dest: Path, *,
                  project_root: Path | None = None) -> int:
    """Copy ``source`` into a NEW ``dest`` that shares nothing with it.

    The whole operation runs under one ``Catalog`` opened on the source. That
    single object is the writer lease, it refuses an interrupted identity
    migration, and its connection is the live handle the snapshot reads -- so
    the lease and the snapshot source cannot drift apart.

    Returns the item count. Raises rather than returning a half-made clone: a
    sandbox completed with one row still pointing at production is worse than no
    sandbox, because it looks safe.
    """
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    db = source / "catalog.db"
    if not db.is_file():
        raise CloneRefused(f"no catalog at {db} -- nothing to sandbox")
    _check_destination(source, dest)

    made_dest = False
    try:
        dest.mkdir(parents=True)
        made_dest = True
        (dest / "media").mkdir()
        # LockBusy from here covers both "another writer holds the source" and
        # "an interrupted migration is pending"; neither is a moving target we
        # may snapshot, and both refuse immediately rather than blocking.
        with Catalog(db) as src:
            con = sqlite3.connect(dest / "catalog.db")
            try:
                # The DEFAULT budget, never RESTORE_TIMEOUT: a capture writes
                # into throwaway space, so failing fast is right. Only a restore
                # onto the live catalog may wait fifteen minutes.
                sqlite_snapshot.backup(src.db, con)
                con.commit()
                for state in state_files(source):
                    shutil.copyfile(state, dest / state.name)
                count = _rebase_media(src, con, source, dest, root)
                con.commit()
            finally:
                con.close()
        write_marker(dest)
        return count
    except BaseException:
        # Only what THIS call created. An occupied destination was refused
        # before any of this, so there is nothing of anyone else's to lose.
        if made_dest:
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _rebase_media(src: Catalog, con: sqlite3.Connection, source: Path,
                  dest: Path, root: Path) -> int:
    """Copy every referenced file in, and repoint every row at its copy.

    Copied, never hard-linked. A hard link makes the clone's bytes the source's
    bytes, so editing the "safe copy" edits the original -- which is the defect,
    not an optimisation of it. Named by content key because a row whose file
    lived outside the source has no relative path to preserve, and content key
    is this project's identity everywhere else.
    """
    count = 0
    for item in src.all_items():
        origin = _resolve_media(item.file_path, source, root)
        out = dest / "media" / (safe_key(item.content_key) + origin.suffix)
        try:
            shutil.copyfile(origin, out)
            copied, original = out.stat().st_size, origin.stat().st_size
        except OSError as exc:
            raise CloneRefused(
                f"cannot copy media for {item.content_key} from {origin}: {exc}") from exc
        if copied != original:
            raise CloneRefused(
                f"media for {item.content_key} copied {copied} of {original} bytes")
        con.execute("UPDATE items SET file_path=? WHERE content_key=?",
                    (out.as_posix(), item.content_key))
        count += 1
    return count
