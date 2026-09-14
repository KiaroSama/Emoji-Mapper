"""Verified filesystem intents and reversible catalog migration snapshots.

The SQLite backup is retained separately. This manifest preserves the original
JSON and exact media bytes by digest; renames use exclusive links, never replace.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from emojikit.packstate import write_json_atomic

from .maintenance import canonical_directory

VERSION = 2
REFERENCES = (("items", "content_key"), ("publications", "content_key"),
              ("seen_files", "content_key"))


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def database_signature(con: sqlite3.Connection, files=()) -> str:
    """Logical rows, including order and identifiers; WAL layout is irrelevant."""
    paths = {f["destination"]: f["source"] for f in files}
    digest = hashlib.sha256()
    schema = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                         "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    for name, sql in schema:
        cur = con.execute(f"SELECT * FROM {_quote(name)}")
        columns = [c[0] for c in cur.description]
        rows = []
        for row in cur:
            row = list(row)
            if name == "items" and "file_path" in columns:
                i = columns.index("file_path")
                row[i] = paths.get(row[i], row[i])
            rows.append(row)
        payload = [name, sql, columns, sorted(rows, key=repr)]
        digest.update(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                 default=lambda value: value.hex()).encode("utf-8"))
    return digest.hexdigest()


def signature(db: Path, files=()) -> str:
    con = sqlite3.connect(db)
    try:
        return database_signature(con, files)
    finally:
        con.close()


def rewrite_database(con, key_map, phashes):
    """Move references in one transaction, including swaps of existing keys."""
    moved = 0
    staged = []
    for i, (old, new) in enumerate(key_map.items()):
        if old == new:
            continue
        temp = f"__identity_migration_{i}__"
        for table, column in REFERENCES:
            if con.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (temp,)).fetchone():
                raise RuntimeError("catalog contains an unresolved temporary identity")
            cur = con.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (temp, old))
            if table == "items":
                moved += cur.rowcount
        staged.append((temp, new))
    for temp, new in staged:
        for table, column in REFERENCES:
            con.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new, temp))
    for key, value in phashes.items():
        con.execute("UPDATE items SET phash=? WHERE content_key=?", (value, key))
    return moved


def plan_files(db: Path, key_map: dict[str, str]) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT content_key, file_path, format FROM items").fetchall()
    finally:
        con.close()
    files = []
    destinations = {}
    for old, stored, fmt in rows:
        source = Path(stored)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"missing or unsupported media path: {source}")
        source = source.resolve()
        new = key_map.get(old, old)
        origin = old
        if old == new:
            predecessors = [key for key, value in key_map.items() if value == old
                            and key.split(":", 1)[1][:12] in source.name]
            if len(predecessors) > 1:
                raise RuntimeError(f"ambiguous recovered filename mapping: {source}")
            if predecessors:
                origin = predecessors[0]
        name = source.name.replace(origin.split(":", 1)[1][:12],
                                   new.split(":", 1)[1][:12])
        dest = source.with_name(name)
        digest = file_digest(source)
        if dest.exists() and (not dest.is_file() or file_digest(dest) != digest):
            raise RuntimeError(f"migration destination contains unrelated bytes: {dest}")
        if str(dest) in destinations and destinations[str(dest)] != old:
            raise RuntimeError(f"two catalog rows would share a destination: {dest}")
        destinations[str(dest)] = old
        files.append({"old_key": old, "key": new, "format": fmt,
                      "filename_key": origin,
                      "source": str(source), "stored_source": stored,
                      "destination": str(dest), "sha256": digest,
                      "destination_existed": dest.exists()})
    return files


def make_bundle(data_dir, backup, key_map, phashes, files, states):
    original = sqlite3.connect(backup)
    expected = sqlite3.connect(":memory:")
    try:
        original.backup(expected)
        before = database_signature(expected)
        rewrite_database(expected, key_map, phashes)
        after = database_signature(expected)
    finally:
        expected.close()
        original.close()
    return {"version": VERSION, "data_dir": str(canonical_directory(data_dir)),
            "catalog": str((Path(data_dir) / "catalog.db").resolve()),
            "backup": str(backup.resolve()), "backup_sha256": file_digest(backup),
            "before_signature": before, "database_signature": after,
            "key_map": key_map, "phash": phashes, "files": files, "states": states,
            "stage": "backup", "direction": "forward"}


def validate_bundle(data_dir: Path, doc: dict) -> None:
    directory = canonical_directory(data_dir)
    if (doc.get("version") != VERSION or doc.get("data_dir") != str(directory)
            or doc.get("catalog") != str((directory / "catalog.db").resolve())):
        raise RuntimeError("migration journal version or catalog binding does not match")
    backup = Path(doc["backup"])
    if backup.parent.resolve() != directory or file_digest(backup) != doc["backup_sha256"]:
        raise RuntimeError("migration backup is missing, modified or belongs elsewhere")
    if signature(backup) != doc["before_signature"]:
        raise RuntimeError("migration backup does not match the recorded snapshot")
    if not isinstance(doc.get("files"), list) or not isinstance(doc.get("states"), dict):
        raise RuntimeError("migration journal has no complete file and JSON intents")
    con = sqlite3.connect(backup)
    try:
        original = {k: (p, fmt) for k, p, fmt in con.execute(
            "SELECT content_key, file_path, format FROM items")}
    finally:
        con.close()
    if len(doc["files"]) != len(original):
        raise RuntimeError("migration journal does not cover the complete snapshot")
    for name in doc["states"]:
        if Path(name).name != name or not name.startswith("publish_") or not name.endswith(".json"):
            raise RuntimeError("migration journal contains an invalid state path")
    for intent in doc["files"]:
        source, dest = Path(intent["source"]), Path(intent["destination"])
        old = intent["old_key"]
        row = original.pop(old, None)
        key = doc["key_map"].get(old, old)
        origin = intent["filename_key"]
        expected_name = source.name.replace(origin.split(":", 1)[1][:12], key.split(":", 1)[1][:12])
        if (row != (intent["stored_source"], intent["format"])
                or (origin != old and doc["key_map"].get(origin) != key)
                or source != Path(intent["stored_source"]).resolve()
                or intent["key"] != key or dest.name != expected_name
                or not source.is_absolute() or source.parent != dest.parent):
            raise RuntimeError("migration file intent is not an evidenced sibling rename")


def verify_files(files, *, final=False):
    for intent in files:
        source, dest = Path(intent["source"]), Path(intent["destination"])
        present = [p for p in {source, dest} if p.exists()]
        if not present or (final and not dest.is_file()):
            raise RuntimeError(f"migration media is missing: {dest}")
        for path in present:
            if path.is_symlink() or not path.is_file() or file_digest(path) != intent["sha256"]:
                raise RuntimeError(f"migration media changed or destination collided: {path}")


def verify_states(data_dir, states):
    names = {p.name for p in Path(data_dir).glob("publish_*.json")}
    if names != set(states):
        raise RuntimeError("publisher state files changed since the migration snapshot")
    for name, values in states.items():
        now = json.loads((Path(data_dir) / name).read_text(encoding="utf-8"))
        if now != values["before"] and now != values["after"]:
            raise RuntimeError(f"publisher state changed after migration: {name}")


def verify_current(data_dir, doc):
    validate_bundle(data_dir, doc)
    verify_files(doc["files"])
    verify_states(data_dir, doc["states"])
    files = [dict(f, source=f["stored_source"]) for f in doc["files"]]
    current = signature(Path(doc["catalog"]), files)
    if current not in (doc["before_signature"], doc["database_signature"]):
        raise RuntimeError("catalog changed outside the migration; refusing to overwrite later data")
    return current


def verify_applied(data_dir, doc):
    if verify_current(data_dir, doc) != doc["database_signature"]:
        raise RuntimeError("migration database edits have not committed")
    verify_files(doc["files"], final=True)
    con = sqlite3.connect(doc["catalog"])
    try:
        paths = dict(con.execute("SELECT content_key, file_path FROM items"))
    finally:
        con.close()
    for intent in doc["files"]:
        if paths.get(intent["key"]) != intent["destination"]:
            raise RuntimeError(f"migration path update is unresolved: {intent['key']}")
    for name, values in doc["states"].items():
        if json.loads((Path(data_dir) / name).read_text(encoding="utf-8")) != values["after"]:
            raise RuntimeError(f"migration state replacement is unresolved: {name}")


def move_file(source: Path, destination: Path, digest: str, *, keep_source=False):
    """Atomic exclusive destination creation, then remove only evidenced bytes."""
    if source == destination:
        return
    if not destination.exists():
        # A sibling hard link works on NTFS and POSIX and fails if the target
        # appears concurrently. os.replace/rename can overwrite it on POSIX.
        os.link(source, destination)
    if file_digest(destination) != digest:
        raise RuntimeError(f"refusing to replace unrelated destination: {destination}")
    if source.exists() and not keep_source:
        if file_digest(source) != digest:
            raise RuntimeError(f"migration source changed: {source}")
        source.unlink()


def apply_files(db, files):
    renamed = []
    for intent in files:
        src, dest = Path(intent["source"]), Path(intent["destination"])
        move_file(src, dest, intent["sha256"])
        con = sqlite3.connect(db)
        try:
            with con:
                con.execute("UPDATE items SET file_path=? WHERE content_key=?",
                            (str(dest), intent["key"]))
        finally:
            con.close()
        if src != dest:
            renamed.append(dest.name)
    return renamed


def restore(data_dir, doc, write_journal):
    """Restore only the recorded before/after states; later edits refuse."""
    verify_current(data_dir, doc)
    doc["direction"] = "restore"
    write_journal(data_dir, doc)
    for intent in reversed(doc["files"]):
        source, dest = Path(intent["source"]), Path(intent["destination"])
        move_file(dest, source, intent["sha256"],
                  keep_source=intent["destination_existed"])
    for name, values in doc["states"].items():
        write_json_atomic(Path(data_dir) / name, values["before"])
    src, dst = sqlite3.connect(doc["backup"]), sqlite3.connect(doc["catalog"])
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if signature(Path(doc["catalog"])) != doc["before_signature"]:
        raise RuntimeError("restored database does not match its backup")
    for intent in doc["files"]:
        if file_digest(Path(intent["source"])) != intent["sha256"]:
            raise RuntimeError("restored media failed byte verification")
    for name, values in doc["states"].items():
        if json.loads((Path(data_dir) / name).read_text(encoding="utf-8")) != values["before"]:
            raise RuntimeError("restored publisher state differs from the snapshot")
    doc["stage"] = "restored"
    write_journal(data_dir, doc)
