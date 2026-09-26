"""Independent catalog copies with ownership spanning creation through cleanup.

The source database is opened read-only under the normal writer lease: merely
constructing Catalog would migrate the owner's source schema and journal mode.
Native lifetime ownership is separate from catalog ownership and survives until
cleanup completes. This is cooperative isolation, not an OS security boundary.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from emojikit import sqlite_snapshot
from emojikit.maintenance import writer
from emojikit.packstate import LockBusy, exclusive_lock, write_json_atomic
from emojikit.state_artifacts import state_files

TMP_PREFIX = "panel-sandbox-"
MARKER_NAME = ".sandbox-owner.json"
# Version 1 used an in-directory lock and had a create/serve ownership gap.
# Older reclaimers skip v2; this reclaimer leaves v1 directories alone rather
# than guessing that an older process has stopped serving one.
MARKER_VERSION = 2
LIFETIME_LOCK_NAME = ".sandbox-lifetime.lock"


class CloneRefused(RuntimeError):
    """The clone cannot be made faithfully, so it is not made at all."""


def lifetime_lock_path(directory: Path) -> Path:
    """Stable external tombstone: deleting a clone never unlinks its lock.

    NTFS cannot delete an open lock file; POSIX can, but that permits a second
    owner to lock a different inode at the same name. A sibling lease directory
    avoids both problems and never becomes a prefix-matched sandbox itself.
    """
    directory = Path(directory).resolve()
    leases = directory.parent / ".panel-sandbox-leases"
    if leases.is_symlink():
        raise CloneRefused(f"unsupported sandbox lease directory: {leases}")
    name = hashlib.sha256(os.path.normcase(str(directory)).encode("utf-8")).hexdigest()
    return leases / (name + LIFETIME_LOCK_NAME)


def safe_key(content_key: str) -> str:
    """An injectively encoded portable name, also for legacy non-hex keys.

    Replacing ':' with '_' aliases distinct input strings. Hex encoding every
    UTF-8 byte cannot alias or introduce path separators or NTFS streams.
    Normal catalog keys remain well below the native filename length limit.
    """
    return content_key.encode("utf-8").hex()


def write_marker(directory: Path) -> Path:
    directory = Path(directory).resolve()
    path = directory / MARKER_NAME
    write_json_atomic(path, {
        "version": MARKER_VERSION,
        "directory": str(directory),
        "created_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),  # Diagnostic only; ownership is the native lock.
    })
    return path


def marker_is_self_describing(directory: Path) -> bool:
    directory = Path(directory)
    marker = directory / MARKER_NAME
    try:
        if directory.is_symlink() or not directory.is_dir() or marker.is_symlink():
            return False
        doc = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or type(doc.get("version")) is not int:
            return False
        named = doc.get("directory")
        return (doc["version"] == MARKER_VERSION and isinstance(named, str)
                and Path(named) == directory.resolve())
    except (OSError, ValueError, TypeError):
        return False


def sweep_stale(root: Path | None = None) -> int:
    """Reclaim evidenced v2 clones only after taking their lifetime lease.

    A marker can disappear while a sweeper is acquiring ownership; recheck it
    under the lease before deleting anything. Unmarked, legacy, unreadable and
    currently owned directories are preserved, never adopted or retried forever.
    """
    import tempfile

    base = Path(tempfile.gettempdir()) if root is None else Path(root)
    removed = 0
    for old in sorted(base.glob(f"{TMP_PREFIX}*")):
        if not marker_is_self_describing(old):
            continue
        try:
            with exclusive_lock(lifetime_lock_path(old)):
                if marker_is_self_describing(old):
                    shutil.rmtree(old)
                    removed += 1
        except (LockBusy, OSError, CloneRefused):
            continue
    return removed


def _check_destination(source: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        raise CloneRefused(f"destination {dest} already exists; refusing to erase it")
    if source == dest or source in dest.parents or dest in source.parents:
        raise CloneRefused(f"destination {dest} overlaps the source {source}")


def _resolve_media(raw: str, source: Path, project_root: Path) -> Path:
    """Absolute archives stay absolute; relative paths mean the project root.

    Searching source/source.parent first can select a different existing file
    from the one the application uses. Never infer path semantics from existence.
    """
    if not isinstance(raw, str) or not raw:
        raise CloneRefused("catalog media path must be a nonempty string")
    path = Path(raw)
    return path if path.is_absolute() else project_root / path


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _rebase_media(con: sqlite3.Connection, source: Path, dest: Path, root: Path) -> int:
    """Read the completed database snapshot, verify and independently copy media."""
    rows = con.execute("SELECT content_key, file_path FROM items").fetchall()
    for key, stored in rows:
        origin = _resolve_media(stored, source, root)
        if not isinstance(key, str) or not key:
            raise CloneRefused("catalog content key must be a nonempty string")
        out = dest / "media" / (safe_key(key) + origin.suffix)
        try:
            before = _digest(origin)
            shutil.copyfile(origin, out)
            # Size equality is not content equality. Also verify the source
            # again: an external archive may not participate in our lease.
            if _digest(out) != before or _digest(origin) != before:
                raise CloneRefused(f"media changed or was not copied faithfully: {origin}")
        except OSError as exc:
            raise CloneRefused(f"cannot copy media for {key} from {origin}: {exc}") from exc
        con.execute("UPDATE items SET file_path=? WHERE content_key=?", (out.as_posix(), key))
    return len(rows)


@contextmanager
def _owned_clone(source: Path, dest: Path, *, project_root: Path | None, discard: bool):
    source, raw_dest = Path(source).resolve(), Path(dest)
    # Check the original path before resolving away a dangling symlink.
    if raw_dest.is_symlink():
        raise CloneRefused(f"destination {raw_dest} is a symbolic link")
    dest = raw_dest.resolve()
    root = (Path(project_root) if project_root is not None
            else Path(__file__).resolve().parent.parent).resolve()
    db = source / "catalog.db"
    if db.is_symlink() or not db.is_file():
        raise CloneRefused(f"no supported catalog at {db} -- nothing to sandbox")
    _check_destination(source, dest)
    with exclusive_lock(lifetime_lock_path(dest)):
        # A simultaneous creator may have won after the preflight.
        _check_destination(source, dest)
        made, ready = False, False
        try:
            dest.mkdir(parents=True, exist_ok=False)
            made = True
            (dest / "media").mkdir()
            with writer(source), closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as src, \
                    closing(sqlite3.connect(dest / "catalog.db")) as con:
                sqlite_snapshot.backup(src, con)
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CloneRefused("sandbox database failed integrity_check")
                for state in state_files(source):
                    shutil.copyfile(state, dest / state.name)
                count = _rebase_media(con, source, dest, root)
                con.commit()
            # Source ownership ends here; the served clone owns no production lease.
            write_marker(dest)
            ready = True
            yield count
        finally:
            if made and (discard or not ready):
                # Remove only this invocation's directory while still owning its
                # external lease. Preserve the lease tombstone itself forever.
                shutil.rmtree(dest)


def clone_catalog(source: Path, dest: Path, *, project_root: Path | None = None) -> int:
    """Make a detached copy; its lifetime lease is released on return.

    A caller that will serve the clone must use sandbox_session instead: there
    must be no reclaimable window between completing a copy and serving it.
    """
    with _owned_clone(source, dest, project_root=project_root, discard=False) as count:
        return count


@contextmanager
def sandbox_session(source: Path, dest: Path, *, project_root: Path | None = None):
    """Own construction, serving and cleanup as one uninterrupted lifetime."""
    with _owned_clone(source, dest, project_root=project_root, discard=True) as count:
        yield count
