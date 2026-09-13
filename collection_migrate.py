"""Moving a content key is a versioned migration, not three SQL updates.

The content key is this project's primary identity, and it is written down in
more places than the table it names. A decode fix that changes what a key IS
therefore has to move every one of them together:

| where | what holds a key |
|---|---|
| `items` | the row id, plus a derived `phash` and a `file_path` whose NAME embeds the key |
| `publications` | the foreign key carrying each `custom_emoji_id` |
| `seen_files` | the foreign key mapping a Telegram `file_unique_id` to an item |
| `publish_<base>.json` | `sets[].keys[]` and `skipped[]` -- what `reconcile_set` attributes live stickers by |
| `publish_plan_<base>.json` | the frozen plan, `{format: [keys]}` |

The first round moved the three tables and stopped. The owner's own state file
was left naming 51 keys that no longer existed, so the next `reconcile_set()`
would have read an untouched live pack as reordered or replaced; the stored
`phash` stayed stale even where a key happened not to move; and the archived
files kept the old key in their names. "Run `pack_archive.py --sync` next" was
the documented finish, which is an unverified manual step, not a migration.

Two invariants make the rest safe:

* **Every stage is idempotent.** Each one is expressed as "make it so", never
  "apply a delta", so an interrupted run is repaired by running it again and a
  second migration over a finished catalog is a verified no-op. That is what
  lets the journal be a breadcrumb rather than a transaction log.
* **Nothing is applied from an incomplete picture.** A file that cannot be read
  is not "unchanged"; if the survey cannot establish what every row should
  become, the migration refuses rather than half-converting.

JSON replacement and a SQL COMMIT are not one atomic transaction and this does
not pretend otherwise. The journal records the intended map and how far the run
got, so a crash between stages is visible and resumable instead of silent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import packstate
from emojikit import identity
# The signed-storage conversion is imported, never re-implemented: a 64-bit
# hash that overflowed SQLite's signed range once dropped rows from this very
# catalog, and a second copy of that arithmetic is how the two drift apart.
from emojikit.catalog import _phash_from_db, _phash_to_db
from emojikit.errors import MediaError

log = logging.getLogger("collection_migrate")

JOURNAL_NAME = "identity-migration.journal.json"
JOURNAL_VERSION = 1

# Every table whose rows are named by a content key. Missing one leaves a
# publication pointing at a row that no longer exists, which is worse than not
# migrating at all.
KEY_REFERENCES = (("items", "content_key"),
                  ("publications", "content_key"),
                  ("seen_files", "content_key"))

STAGES = ("backup", "database", "state", "files")


@dataclass
class Survey:
    """What every video row currently is, and what it should become.

    The counters are separate on purpose. A row whose file is missing is not
    "unchanged" -- it is a row we could not inspect, and the previous version
    reported exactly that case as a clean success.
    """

    checked: int = 0
    unchanged: int = 0
    changed: list[tuple[str, str, str]] = field(default_factory=list)
    phash_only: list[tuple[str, object, object]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    undecodable: list[str] = field(default_factory=list)
    collisions: dict[str, list[str]] = field(default_factory=dict)
    # FINAL key -> freshly computed hash, for every row that was inspected.
    # Keyed on where the row ENDS UP, because a row whose key moves needs its
    # derived hash written just as much as one whose key holds still -- leaving
    # that out is how the re-survey kept reporting work after a clean run.
    phashes: dict[str, object] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Did we manage to inspect everything we were asked about?"""
        return not self.missing and not self.undecodable

    @property
    def pending(self) -> bool:
        return bool(self.changed or self.phash_only or self.collisions)

    @property
    def key_map(self) -> dict[str, str]:
        return {old: new for old, new, _name in self.changed}


def catalog_path(data_dir: Path) -> Path:
    db = data_dir / "catalog.db"
    if not db.is_file():
        raise FileNotFoundError(f"no catalog at {db}")
    return db


def survey(data_dir: Path) -> Survey:
    """Recompute every video row's identity, touching nothing.

    Also recomputes the derived perceptual hash, because a decoder change moves
    that even where the exact key happens to land in the same place -- the
    previous version left a fixture holding `123` when the current decoder
    returned `0`, and that hash is what near-duplicate search reads.
    """
    db = catalog_path(data_dir)
    out = Survey()
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT content_key, file_path, phash FROM items WHERE format='video'"
        ).fetchall()
        existing = {r[0] for r in con.execute("SELECT content_key FROM items")}
    finally:
        con.close()

    for old, path, stored in rows:
        out.checked += 1
        p = Path(path)
        if not p.is_file():
            out.missing.append(f"{old}: {p}")
            continue
        try:
            fresh, phash = identity.fingerprint(p, "video")
        except (MediaError, OSError, ValueError) as exc:
            out.undecodable.append(f"{old}: {type(exc).__name__}: {exc}")
            continue
        stored_signed = _phash_from_db(stored)
        out.phashes[fresh] = phash
        if fresh != old:
            out.changed.append((old, fresh, p.name))
        elif phash != stored_signed:
            out.phash_only.append((old, stored_signed, phash))
        else:
            out.unchanged += 1

    out.collisions = _collisions(out, existing)
    return out


def _collisions(sv: Survey, existing: set[str]) -> dict[str, list[str]]:
    """New keys that two rows would claim, or that a row already holds.

    Either case means two catalog rows are about to become one identity, and
    whichever lost would take its media and its publication history with it.
    """
    by_new: dict[str, list[str]] = {}
    for old, new, _name in sv.changed:
        by_new.setdefault(new, []).append(old)
    moving = set(sv.key_map)
    clashes = {new: olds for new, olds in by_new.items() if len(olds) > 1}
    for new, olds in by_new.items():
        if new in existing and new not in moving:
            clashes.setdefault(new, list(olds)).append("(already in the catalog)")
    return clashes


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

def backup_catalog(db: Path) -> Path:
    """A consistent snapshot, via SQLite's online backup API.

    `shutil.copy2` copies the main database file and nothing else. Under WAL --
    which is how this catalog runs -- committed rows live in `-wal` until a
    checkpoint, so the copy is a database that never had them: opening the
    previous version's "backup" with a writer connection still open reported
    `no such table: items` while the original held the row. A backup that
    cannot be restored is not a backup, so this one is verified before the
    caller is allowed to change anything.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for attempt in range(64):
        # Collision-resistant, and it REFUSES rather than overwriting: two runs
        # in the same second must not silently share one snapshot.
        suffix = "" if attempt == 0 else f"-{attempt}"
        dest = db.with_name(f"catalog.before-video-identity-{stamp}{suffix}.db")
        if not dest.exists():
            break
    else:
        raise RuntimeError(f"cannot find an unused backup name beside {db}")

    src = sqlite3.connect(db)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    _verify_backup(db, dest)
    return dest


def _verify_backup(db: Path, dest: Path) -> None:
    """Reopen it, check it, and compare it with what it claims to copy."""
    con = sqlite3.connect(dest)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup {dest.name} failed integrity_check: {ok}")
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t, _c in KEY_REFERENCES}
    except sqlite3.Error as exc:
        raise RuntimeError(f"backup {dest.name} is not readable: {exc}") from exc
    finally:
        con.close()

    live = sqlite3.connect(db)
    try:
        want = {t: live.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t, _c in KEY_REFERENCES}
    finally:
        live.close()
    if counts != want:
        raise RuntimeError(
            f"backup {dest.name} holds {counts} rows but the catalog holds "
            f"{want}; refusing to migrate against a snapshot that differs")


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

def journal_path(data_dir: Path) -> Path:
    return data_dir / JOURNAL_NAME


def read_journal(data_dir: Path) -> dict | None:
    p = journal_path(data_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{p} exists but cannot be read ({exc}). A migration was "
            f"interrupted; inspect it deliberately rather than deleting it") from exc


def _write_journal(data_dir: Path, doc: dict) -> None:
    packstate.write_json_atomic(journal_path(data_dir), doc)


# --------------------------------------------------------------------------- #
# The durable edits, each idempotent
# --------------------------------------------------------------------------- #

def _apply_database(db: Path, key_map: dict[str, str],
                    phashes: dict[str, object]) -> int:
    """Move every key reference, and refresh every derived hash, in ONE commit."""
    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        moved = 0
        for old, new in key_map.items():
            for table, column in KEY_REFERENCES:
                cur = con.execute(
                    f"UPDATE {table} SET {column}=? WHERE {column}=?", (new, old))
                if table == "items":
                    moved += cur.rowcount
        for key, phash in phashes.items():
            con.execute("UPDATE items SET phash=? WHERE content_key=?",
                        (_phash_to_db(phash), key))
        con.commit()
        return moved
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def state_files(data_dir: Path) -> list[Path]:
    """Every publisher artifact that names content keys."""
    return sorted(data_dir.glob("publish_*.json"))


def _remap(node, key_map: dict[str, str]):
    """Replace keys anywhere in a JSON document, preserving its shape."""
    if isinstance(node, str):
        return key_map.get(node, node)
    if isinstance(node, list):
        return [_remap(v, key_map) for v in node]
    if isinstance(node, dict):
        return {k: _remap(v, key_map) for k, v in node.items()}
    return node


def _apply_state(data_dir: Path, key_map: dict[str, str]) -> list[str]:
    """Rewrite the state and plan files. Idempotent: a key already moved is
    simply not in the map any more, so a second pass changes nothing."""
    touched = []
    for path in state_files(data_dir):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"{path.name} could not be read ({exc}); refusing to leave it "
                f"naming keys the catalog no longer has") from exc
        fresh = _remap(doc, key_map)
        if fresh != doc:
            packstate.write_json_atomic(path, fresh)
            touched.append(path.name)
    return touched


def _apply_files(db: Path, key_map: dict[str, str]) -> list[str]:
    """Rename archived media whose NAME embeds the old key, and record it.

    The archive names a file `<slot>_<format>_<key[:12]>.<ext>`, so a moved key
    leaves every archived file misnamed. Doing it here rather than telling the
    owner to run the archive tool afterwards is the difference between a
    migration and a migration plus an unverified manual step -- and it needs no
    network, because the rename is decided entirely by the map.
    """
    renamed = []
    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        for old, new in key_map.items():
            row = con.execute("SELECT file_path FROM items WHERE content_key=?",
                              (new,)).fetchone()
            if not row:
                continue
            path = Path(row[0])
            want_stem = path.name.replace(old.split(":", 1)[1][:12],
                                          new.split(":", 1)[1][:12])
            if want_stem == path.name:
                continue
            dest = path.with_name(want_stem)
            if path.is_file():
                os.replace(path, dest)
                renamed.append(dest.name)
            elif not dest.is_file():
                continue          # nothing on disk either way; leave the row
            con.execute("UPDATE items SET file_path=? WHERE content_key=?",
                        (str(dest), new))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return renamed


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def _bases(data_dir: Path) -> list[str]:
    """Pack families whose state this migration may touch."""
    out = []
    for p in data_dir.glob("publish_*.json"):
        name = p.stem
        if name.startswith("publish_plan_"):
            name = name[len("publish_plan_"):]
        else:
            name = name[len("publish_"):]
        if name:
            out.append(name)
    return sorted(set(out))


def apply_migration(data_dir: Path, sv: Survey,
                    recovered: dict[str, str] | None = None) -> dict:
    """Do it, under the writer exclusion every other tool honours.

    The pack-family lock is the project's writer-exclusion protocol: ingest,
    publishing, the panel's saves and the archive all take it, so taking it here
    is what makes this safe against them. A migration-only lock nobody else
    honours would be decoration.
    """
    db = catalog_path(data_dir)
    with ExitStack() as stack:
        for base in _bases(data_dir):
            stack.enter_context(
                packstate.exclusive_lock(packstate.pack_family_lock_path(base)))

        doc = read_journal(data_dir) or {}

        # A resumed run CANNOT re-derive the map by surveying. Once the
        # database stage has committed, the rows already hold their new keys,
        # so a fresh survey reports nothing pending -- while the state files
        # still name the old ones. The journal's map is the only record that
        # those two facts belong together, which is the whole reason it is
        # written before the first durable change. Merged, not replaced: a
        # resume may also have new work of its own.
        key_map = {**(recovered or {}), **doc.get("key_map", {}), **sv.key_map}
        phashes = {**doc.get("phash", {}), **sv.phashes}

        doc.update({"version": JOURNAL_VERSION,
                    "started_utc": doc.get("started_utc")
                    or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "key_map": key_map,
                    "phash": phashes,
                    "stage": doc.get("stage", "planned")})
        _write_journal(data_dir, doc)

        result = {"backup": doc.get("backup"), "moved": 0,
                  "state": [], "renamed": []}
        if not doc.get("backup"):
            result["backup"] = str(backup_catalog(db))
            doc["backup"] = result["backup"]
            doc["stage"] = "backup"
            _write_journal(data_dir, doc)

        result["moved"] = _apply_database(db, key_map, phashes)
        doc["stage"] = "database"
        _write_journal(data_dir, doc)

        result["state"] = _apply_state(data_dir, key_map)
        doc["stage"] = "state"
        _write_journal(data_dir, doc)

        result["renamed"] = _apply_files(db, key_map)
        doc["stage"] = "files"
        _write_journal(data_dir, doc)

    journal_path(data_dir).unlink(missing_ok=True)
    return result


def recover_key_map(data_dir: Path, backup: Path) -> dict[str, str]:
    """Reconstruct old -> new for a migration that ran before journals existed.

    The first round moved the table rows and kept no record of the map, so the
    state files it left behind cannot be repaired by surveying: the catalog
    already holds the new keys, so a fresh survey reports nothing pending while
    the state still names the old ones. The backup that round DID write is the
    missing half, and pairing it with the live catalog recovers the map.

    Content-based, never positional -- position-based mapping is the defect
    this project has already been bitten by twice. A row is paired by the part
    of its identity the migration did not invent:

    * the same `file_path` in both catalogs is the same row (media that was
      never archived, so nothing renamed it), then
    * the same path once each side's OWN key prefix is blanked out, which is
      exactly what the archive rename changes and nothing else.

    An old row that matches more than one live row is left unmapped rather than
    guessed at, and the caller checks coverage before applying anything.
    """
    def _rows(db: Path):
        con = sqlite3.connect(db)
        try:
            return con.execute(
                "SELECT content_key, file_path FROM items WHERE format='video'"
            ).fetchall()
        finally:
            con.close()

    def _shape(key: str, path: str) -> str:
        return path.replace(key.split(":", 1)[1][:12], "\0KEY\0")

    live = _rows(catalog_path(data_dir))
    by_path: dict[str, list[str]] = {}
    by_shape: dict[str, list[str]] = {}
    for key, path in live:
        by_path.setdefault(path, []).append(key)
        by_shape.setdefault(_shape(key, path), []).append(key)

    mapping: dict[str, str] = {}
    for key, path in _rows(backup):
        found = by_path.get(path) or by_shape.get(_shape(key, path)) or []
        if len(found) == 1 and found[0] != key:
            mapping[key] = found[0]
    return mapping


def stale_state_keys(data_dir: Path) -> dict[str, list[str]]:
    """Keys named by a state or plan file that the catalog does not have.

    The completeness check the first round lacked: it is what proves a
    migration finished, and what a second run reads to confirm a no-op.
    """
    db = catalog_path(data_dir)
    con = sqlite3.connect(db)
    try:
        known = {r[0] for r in con.execute("SELECT content_key FROM items")}
    finally:
        con.close()

    out: dict[str, list[str]] = {}
    for path in state_files(data_dir):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[path.name] = ["<unreadable>"]
            continue
        found: set[str] = set()
        _collect_keys(doc, found)
        stale = sorted(k for k in found if k not in known)
        if stale:
            out[path.name] = stale
    return out


def _collect_keys(node, into: set[str]) -> None:
    if isinstance(node, str):
        if len(node) > 2 and node[1] == ":" and node[0] in "sva":
            into.add(node)
    elif isinstance(node, list):
        for v in node:
            _collect_keys(v, into)
    elif isinstance(node, dict):
        for v in node.values():
            _collect_keys(v, into)
