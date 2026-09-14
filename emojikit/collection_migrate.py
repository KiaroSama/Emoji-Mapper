"""A catalog identity migration owns every writer and every durable reference.

The canonical data-directory lock spans survey, backup, JSON/media/SQL changes,
verification and journal retirement. The SQLite snapshot plus rollback manifest
preserve the complete prior application state. Evidenced per-file intents replay
before survey so an interrupted rename cannot strand the old database path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from emojikit import packstate
from emojikit import identity
from emojikit import migration_bundle as bundle
from emojikit.maintenance import JOURNAL_NAME, maintenance
# The signed-storage conversion is imported, never re-implemented: a 64-bit
# hash that overflowed SQLite's signed range once dropped rows from this very
# catalog, and a second copy of that arithmetic is how the two drift apart.
from emojikit.catalog import _phash_from_db, _phash_to_db
from emojikit.errors import MediaError

log = logging.getLogger("emojikit.collection_migrate")

JOURNAL_VERSION = bundle.VERSION

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
            if old.count(":") == 2:
                fresh = identity.preserve_collision_key(p, fresh, old)
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
        try:
            dest.open("xb").close()
            break
        except FileExistsError:
            continue
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
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not doc:
            raise ValueError("journal must contain a migration object")
        return doc
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
        moved = bundle.rewrite_database(
            con, key_map, {key: _phash_to_db(value) for key, value in phashes.items()})
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


def _apply_state(data_dir: Path, key_map: dict[str, str], states=None) -> list[str]:
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
        if states is not None:
            fresh = states[path.name]["after"]
        if fresh != doc:
            packstate.write_json_atomic(path, fresh)
            touched.append(path.name)
    return touched


def _apply_files(db: Path, files: list[dict]) -> list[str]:
    """Replay durable, verified source/destination intents before re-survey."""
    return bundle.apply_files(db, files)


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


def apply_migration(data_dir: Path, sv: Survey | None = None,
                    recovered: dict[str, str] | None = None) -> dict:
    """Own discovery through final verification and retirement, across writers."""
    from emojikit.collection_state import _lock_path
    with maintenance(data_dir) as data_dir, ExitStack() as stack:
        db = catalog_path(data_dir)
        for base in _bases(data_dir):
            stack.enter_context(packstate.exclusive_lock(_lock_path(data_dir, base)))
            stack.enter_context(
                packstate.exclusive_lock(packstate.pack_family_lock_path(base)))
        doc = read_journal(data_dir)
        if doc is None:
            # Never trust a caller's pre-lock survey: a writer may have changed
            # any key, path or plan between its discovery and this ownership.
            sv = survey(data_dir)
            if not sv.complete or sv.collisions:
                raise RuntimeError("migration survey is incomplete or contains collisions")
            key_map = {**(recovered or {}), **sv.key_map}
            required = required_state_keys(data_dir)
            uncovered = {k for keys in required.values() for k in keys} - set(key_map)
            if uncovered:
                raise RuntimeError("required state references have no trusted mapping: "
                                   + ", ".join(sorted(uncovered)))
            files = bundle.plan_files(db, key_map)
            states = {}
            for path in state_files(data_dir):
                original = json.loads(path.read_text(encoding="utf-8"))
                states[path.name] = {"before": original, "after": _remap(original, key_map)}
            backup = backup_catalog(db)
            phashes = {key: _phash_to_db(value) for key, value in sv.phashes.items()}
            doc = bundle.make_bundle(data_dir, backup, key_map, phashes, files, states)
            doc["started_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            doc["bundle"] = str(backup.with_suffix(".rollback.json"))
            packstate.write_json_atomic(Path(doc["bundle"]), doc)
            _write_journal(data_dir, doc)
        current = bundle.verify_current(data_dir, doc)
        if doc.get("direction") == "restore":
            raise RuntimeError("rollback is interrupted; re-run restore --apply with its bundle")
        result = {"backup": doc["backup"], "bundle": doc["bundle"], "moved": 0,
                  "state": [], "renamed": [], "key_map": doc["key_map"]}
        if current == doc["before_signature"]:
            result["moved"] = _apply_database(db, doc["key_map"], doc["phash"])
        doc["stage"] = "database"
        _write_journal(data_dir, doc)
        result["state"] = _apply_state(data_dir, doc["key_map"], doc["states"])
        doc["stage"] = "state"
        _write_journal(data_dir, doc)
        result["renamed"] = _apply_files(db, doc["files"])
        doc["stage"] = "files"
        _write_journal(data_dir, doc)
        bundle.verify_applied(data_dir, doc)
        issues = invariant_issues(data_dir, moved=set(doc["key_map"]))
        if issues:
            raise RuntimeError("migration final verification failed: " + "; ".join(issues))
        doc["stage"] = "verified"
        _write_journal(data_dir, doc)
        packstate.write_json_atomic(Path(doc["bundle"]), doc)
        journal_path(data_dir).unlink()
        return result


def restore_migration(data_dir: Path, manifest: Path, *, apply: bool) -> None:
    from emojikit.collection_state import _lock_path
    with maintenance(data_dir) as directory, ExitStack() as stack:
        for base in _bases(directory):
            stack.enter_context(packstate.exclusive_lock(_lock_path(directory, base)))
            stack.enter_context(packstate.exclusive_lock(packstate.pack_family_lock_path(base)))
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        existing = read_journal(directory)
        if existing and Path(existing.get("bundle", "")).resolve() != manifest.resolve():
            raise RuntimeError("a different migration journal is still pending")
        bundle.verify_current(directory, doc)
        if not apply:
            return
        bundle.restore(directory, doc, _write_journal)
        packstate.write_json_atomic(manifest, doc)
        journal_path(directory).unlink()


def recover_key_map(data_dir: Path, backup: Path) -> dict[str, str]:
    """Recover legacy keys only from shared, unambiguous immutable identifiers.

    Paths and archive slots are mutable. Recomputing the current file only
    verifies its current key; it says nothing about the backup row's identity.
    FUID/CID associations must agree across snapshots before that verification.
    Missing or conflicting provenance stays uncovered for the caller to refuse.
    """
    def snapshot(db: Path):
        con = sqlite3.connect(db)
        try:
            rows = dict(con.execute(
                "SELECT content_key, file_path FROM items WHERE format='video'"))
            identifiers = {}

            def collect(kind, query):
                for key, value in con.execute(query):
                    if isinstance(value, (str, int)) and str(value).strip():
                        identifiers.setdefault((kind, str(value)), set()).add(key)

            collect("fuid", "SELECT content_key, file_unique_id FROM seen_files")
            collect("cid", "SELECT content_key, custom_emoji_id FROM publications")
            if any(row[1] == "custom_emoji_id" for row in con.execute("PRAGMA table_info(items)")):
                collect("cid", "SELECT content_key, custom_emoji_id FROM items")
            return rows, identifiers
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot verify legacy identifier provenance in {db.name}: {exc}") from exc
        finally:
            con.close()

    previous, old_ids = snapshot(backup)
    current, current_ids = snapshot(catalog_path(data_dir))
    old_targets, current_sources = {}, {}
    ambiguous_old, ambiguous_current = set(), set()
    for identifier in old_ids.keys() & current_ids.keys():
        sources, targets = old_ids[identifier], current_ids[identifier]
        if len(sources) != 1 or len(targets) != 1:
            ambiguous_old.update(sources)
            ambiguous_current.update(targets)
            continue
        source, target = next(iter(sources)), next(iter(targets))
        old_targets.setdefault(source, set()).add(target)
        current_sources.setdefault(target, set()).add(source)

    mapping = {}
    for key in previous:
        candidates = old_targets.get(key, set())
        if key in ambiguous_old or len(candidates) != 1:
            continue
        target = next(iter(candidates))
        if (target == key or target not in current or target in ambiguous_current
                or current_sources.get(target) != {key}):
            continue
        path = Path(current[target])
        try:
            fresh, _ = identity.fingerprint(path, "video")
            if target.count(":") == 2:
                fresh = identity.preserve_collision_key(path, fresh, target)
        except (MediaError, OSError, ValueError):
            continue
        if fresh == target:
            mapping[key] = target
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


def required_state_keys(data_dir: Path) -> dict[str, list[str]]:
    """Missing live/in-flight keys; obsolete frozen-plan/skipped history stays."""
    con = sqlite3.connect(catalog_path(data_dir))
    try:
        known = {r[0] for r in con.execute("SELECT content_key FROM items")}
    finally:
        con.close()
    out = {}
    for path in state_files(data_dir):
        if path.name.startswith("publish_plan_"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[path.name] = ["<unreadable>"]
            continue
        if not isinstance(doc, dict):
            out[path.name] = ["<invalid state>"]
            continue
        found = set()
        _collect_keys({k: v for k, v in doc.items() if k != "skipped"}, found)
        stale = sorted(found - known)
        if stale:
            out[path.name] = stale
    return out


def invariant_issues(data_dir: Path, *, moved=frozenset()) -> list[str]:
    """Recompute under ownership; missing evidence cannot certify completion."""
    issues = []
    sv = survey(data_dir)
    if not sv.complete or sv.pending:
        issues.append(f"survey: missing={len(sv.missing)}, undecodable={len(sv.undecodable)}, "
                      f"changed={len(sv.changed)}, hashes={len(sv.phash_only)}, "
                      f"collisions={len(sv.collisions)}")
    for name, keys in required_state_keys(data_dir).items():
        issues.append(f"required references in {name}: {', '.join(keys)}")
    for name, keys in stale_state_keys(data_dir).items():
        left = sorted(set(keys) & moved)
        if left or "<unreadable>" in keys:
            issues.append(f"unresolved references in {name}: {', '.join(left or keys)}")
    con = sqlite3.connect(catalog_path(data_dir))
    try:
        for table in ("publications", "seen_files"):
            orphan = con.execute(f"SELECT content_key FROM {table} WHERE content_key "
                                 "NOT IN (SELECT content_key FROM items)").fetchall()
            if orphan:
                issues.append(f"{table} has {len(orphan)} orphaned reference(s)")
        rows = con.execute("SELECT content_key, file_path, format FROM items").fetchall()
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            issues.append(f"database integrity: {check}")
    finally:
        con.close()
    for key, path, fmt in rows:
        p = Path(path)
        if not p.is_file():
            issues.append(f"missing catalog media: {key}")
        elif fmt != "video":
            try:
                fresh, _ = identity.fingerprint(p, fmt)
                if key.count(":") == 2:
                    fresh = identity.preserve_collision_key(p, fresh, key)
                if fresh != key:
                    issues.append(f"non-video identity mismatch: {key}")
            except (MediaError, OSError, ValueError) as exc:
                issues.append(f"non-video identity undecodable: {key}: {exc}")
    return issues


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
