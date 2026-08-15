"""Content-addressed emoji catalog -- the duplicate-proof core.

The catalog is a small SQLite database that stores every prepared emoji exactly
once, keyed by a normalized *content hash* (see :func:`emojikit.media.content_key`).
It is the single source of truth for both ingest paths ("download from Telegram"
and "build from scratch") and for publishing.

Why this design eliminates the old duplicate pain:

1. **Pre-dedup by ``file_unique_id``** -- Telegram returns a stable
   ``file_unique_id`` per sticker. If we have already ingested it we skip the
   download entirely (no wasted bandwidth/time).
2. **Exact dedup by content hash** -- identical media from different packs
   collapse onto one row; their emoji/keywords/sources are merged.
3. **Near-dup by perceptual hash** -- visually identical logos that differ only
   by re-compression are merged within a configurable Hamming threshold.
4. **Idempotent publishing** -- once a row is uploaded its ``custom_emoji_id`` is
   stored, so re-running never re-uploads. No more delete-and-rebuild cycles.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import media

log = logging.getLogger("emojikit.catalog")

SCHEMA_VERSION = 1
# Near-duplicate (perceptual) merging is OFF by default: faithfully copying a
# pack must keep visually-similar-but-DISTINCT emoji. Dedup then relies on exact
# content (normalized pixels) + file_unique_id only. Set a >=0 Hamming threshold
# (e.g. via --phash-threshold) to opt in to merging near-identical images.
DEFAULT_PHASH_THRESHOLD = -1


@dataclass
class Item:
    content_key: str
    fmt: str
    file_path: str
    emojis: list[str]
    keywords: list[str]
    sources: list[str]
    phash: int | None
    custom_emoji_id: str | None
    uploaded: bool
    included: bool = True


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# A 64-bit dHash is unsigned and can exceed SQLite's signed-64-bit INTEGER max.
# Store it as a signed 64-bit value (two's complement) and restore on read so it
# fits the driver without a schema migration.
_U64 = (1 << 64) - 1


def _phash_to_db(p: int | None) -> int | None:
    if p is None:
        return None
    p &= _U64
    return p - (1 << 64) if p >= (1 << 63) else p


def _phash_from_db(v) -> int | None:
    if v is None:
        return None
    return int(v) & _U64


class Catalog:
    """SQLite-backed, content-addressed emoji catalog."""

    def __init__(self, db_path: Path, *, phash_threshold: int = DEFAULT_PHASH_THRESHOLD):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.phash_threshold = phash_threshold
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    # ----- lifecycle ----------------------------------------------------- #
    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                content_key      TEXT PRIMARY KEY,
                format           TEXT NOT NULL,
                file_path        TEXT NOT NULL,
                emojis           TEXT NOT NULL DEFAULT '[]',
                keywords         TEXT NOT NULL DEFAULT '[]',
                sources          TEXT NOT NULL DEFAULT '[]',
                phash            INTEGER,
                custom_emoji_id  TEXT,
                uploaded         INTEGER NOT NULL DEFAULT 0,
                included         INTEGER NOT NULL DEFAULT 1,
                created_utc      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_format ON items(format);
            CREATE INDEX IF NOT EXISTS idx_items_uploaded ON items(uploaded);
            CREATE TABLE IF NOT EXISTS seen_files (
                file_unique_id TEXT PRIMARY KEY,
                content_key    TEXT NOT NULL
            );
            """
        )
        # Migrate older databases that predate the 'included' column.
        try:
            self.db.execute("ALTER TABLE items ADD COLUMN included INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migrate older databases that predate the 'position' column (manual
        # publish order, editable from the curate panel). Seed existing rows to
        # their insertion order (rowid) so ordering is stable and NULL-free.
        try:
            self.db.execute("ALTER TABLE items ADD COLUMN position INTEGER")
            self.db.execute("UPDATE items SET position = rowid WHERE position IS NULL")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.db.commit()

    def close(self) -> None:
        # Idempotent: safe to call more than once (e.g. context manager + test).
        try:
            self.db.commit()
            self.db.close()
        except sqlite3.ProgrammingError:
            pass

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- fast pre-dedup ------------------------------------------------ #
    def seen_file_unique_id(self, fuid: str) -> str | None:
        """Return the content_key a Telegram file_unique_id maps to, if known."""
        row = self.db.execute(
            "SELECT content_key FROM seen_files WHERE file_unique_id=?", (fuid,)
        ).fetchone()
        return row["content_key"] if row else None

    def _record_seen(self, fuid: str, content_key: str) -> None:
        """Map a Telegram file_unique_id to a catalog item, first mapping wins.

        A file_unique_id identifies one file on Telegram, so it must not point
        at two different items. INSERT OR REPLACE silently reassigned it, which
        makes the fast pre-dedup resolve a sticker to the wrong emoji; keep the
        original and report the conflict instead.
        """
        existing = self.seen_file_unique_id(fuid)
        if existing is not None:
            if existing != content_key:
                log.warning(
                    "file_unique_id %s already maps to %s; refusing to "
                    "reassign it to %s", fuid, existing, content_key)
            return
        self.db.execute(
            "INSERT INTO seen_files(file_unique_id, content_key) VALUES(?, ?)",
            (fuid, content_key),
        )

    def record_file_unique_id(self, fuid: str, content_key: str) -> None:
        """Record a Telegram ``file_unique_id`` for an EXISTING catalog item.

        Used after publishing: the uploaded copy of an item gets its own
        file_unique_id on Telegram. Recording it means a later fetch of our
        own pack (or of custom-emoji ids pointing into it) is recognized by
        the fast pre-dedup and never downloaded again.
        """
        if not fuid or self.get(content_key) is None:
            return
        self._record_seen(fuid, content_key)
        self.db.commit()

    # ----- ingest -------------------------------------------------------- #
    def _find_near_duplicate(self, fmt: str, phash: int | None) -> str | None:
        """Return an existing content_key whose perceptual hash is within the
        configured Hamming threshold of ``phash`` (same format only)."""
        if phash is None or self.phash_threshold < 0:
            return None
        rows = self.db.execute(
            "SELECT content_key, phash FROM items WHERE format=? AND phash IS NOT NULL",
            (fmt,),
        ).fetchall()
        for r in rows:
            if media.hamming(_phash_from_db(r["phash"]), phash) <= self.phash_threshold:
                return r["content_key"]
        return None

    def add(self, *, content_key: str, fmt: str, file_path: Path,
            emojis: list[str] | None = None, keywords: list[str] | None = None,
            source: str | None = None, phash: int | None = None,
            file_unique_id: str | None = None) -> tuple[str, bool]:
        """Insert or merge an emoji into the catalog.

        Returns ``(canonical_key, is_new)``. If an exact or near-duplicate
        already exists, the new emoji/keywords/source are merged into it and the
        canonical key of the existing row is returned.
        """
        emojis = emojis or []
        keywords = keywords or []

        existing = self.db.execute(
            "SELECT content_key FROM items WHERE content_key=?", (content_key,)
        ).fetchone()
        canonical = existing["content_key"] if existing else None

        if canonical is None:
            near = self._find_near_duplicate(fmt, phash)
            if near is not None:
                log.info("near-duplicate of %s -> merging (key %s)", near, content_key)
                canonical = near

        if canonical is not None:
            self._merge(canonical, emojis, keywords, source)
            if file_unique_id:
                self._record_seen(file_unique_id, canonical)
            self.db.commit()
            self._drop_unreferenced(canonical, file_path)
            return canonical, False

        next_pos = self.db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM items").fetchone()[0]
        self.db.execute(
            "INSERT INTO items(content_key, format, file_path, emojis, keywords, "
            "sources, phash, uploaded, created_utc, position) VALUES(?,?,?,?,?,?,?,0,?,?)",
            (content_key, fmt, str(file_path), json.dumps(emojis),
             json.dumps(keywords), json.dumps([source] if source else []),
             _phash_to_db(phash), _now(), next_pos),
        )
        if file_unique_id:
            self._record_seen(file_unique_id, content_key)
        self.db.commit()
        log.debug("new %s item %s (%s)", fmt, content_key, file_path)
        return content_key, True

    def _drop_unreferenced(self, canonical: str, file_path: Path) -> None:
        """Delete a just-ingested media file that lost a merge.

        Callers move the file into permanent storage BEFORE add() decides, so a
        merge (exact or near-duplicate) leaves that file on disk with no row
        pointing at it. Only the losing copy is removed, never the canonical
        row's own file.
        """
        row = self.db.execute(
            "SELECT file_path FROM items WHERE content_key=?", (canonical,)
        ).fetchone()
        if row is None:
            return
        try:
            kept, losing = Path(row["file_path"]).resolve(), Path(file_path).resolve()
        except OSError:
            return
        if kept == losing or not losing.is_file():
            return
        try:
            losing.unlink()
            log.debug("removed unreferenced media %s (merged into %s)",
                      losing.name, canonical)
        except OSError as exc:
            log.warning("could not remove unreferenced media %s: %s", losing, exc)

    def _merge(self, key: str, emojis: list[str], keywords: list[str],
               source: str | None) -> None:
        row = self.db.execute(
            "SELECT emojis, keywords, sources FROM items WHERE content_key=?", (key,)
        ).fetchone()
        cur_e = json.loads(row["emojis"]); cur_k = json.loads(row["keywords"])
        cur_s = json.loads(row["sources"])
        merged_e = _merge_unique(cur_e, emojis)
        merged_k = _merge_unique(cur_k, keywords)
        merged_s = _merge_unique(cur_s, [source] if source else [])
        self.db.execute(
            "UPDATE items SET emojis=?, keywords=?, sources=? WHERE content_key=?",
            (json.dumps(merged_e), json.dumps(merged_k), json.dumps(merged_s), key),
        )

    # ----- publishing ---------------------------------------------------- #
    def pending(self, fmt: str | None = None) -> list[Item]:
        """Items not yet uploaded AND included, in deterministic (frozen) order."""
        if fmt:
            rows = self.db.execute(
                "SELECT * FROM items WHERE uploaded=0 AND included=1 AND format=? "
                "ORDER BY position, content_key", (fmt,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM items WHERE uploaded=0 AND included=1 "
                "ORDER BY position, content_key"
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def all_items(self, fmt: str | None = None) -> list[Item]:
        """Every catalog item (any state) in the manual publish order (position).

        The curate panel shows and lets you drag-reorder items in this order;
        the same order drives publishing (per-format sets keep this relative
        order). ``content_key`` is a stable tiebreak.
        """
        if fmt:
            rows = self.db.execute(
                "SELECT * FROM items WHERE format=? ORDER BY position, content_key",
                (fmt,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM items ORDER BY position, content_key").fetchall()
        return [_row_to_item(r) for r in rows]

    def set_order(self, ordered_keys: list[str]) -> int:
        """Persist a manual order: position = index for each given content_key.

        Keys not present are ignored; items not listed keep their old position
        but are pushed after the listed ones (their position is offset). Returns
        the number of items whose position was set.
        """
        n = 0
        for i, key in enumerate(ordered_keys):
            cur = self.db.execute(
                "UPDATE items SET position=? WHERE content_key=?", (i, key))
            n += cur.rowcount
        # Any item not in the list goes after, preserving its relative order.
        base = len(ordered_keys)
        self.db.execute(
            "UPDATE items SET position = position + ? "
            "WHERE content_key NOT IN (%s)" % (",".join("?" * len(ordered_keys)) or "''"),
            [base] + list(ordered_keys) if ordered_keys else [base],
        )
        self.db.commit()
        return n

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self.db.commit()

    def set_inclusion(self, excluded_keys: set[str]) -> tuple[int, int]:
        """Mark the given keys as excluded (included=0) and all others included=1.

        Returns (included_count, excluded_count).
        """
        self.db.execute("UPDATE items SET included=1")
        if excluded_keys:
            self.db.executemany("UPDATE items SET included=0 WHERE content_key=?",
                                [(k,) for k in excluded_keys])
        self.db.commit()
        inc = self.db.execute("SELECT COUNT(*) FROM items WHERE included=1").fetchone()[0]
        exc = self.db.execute("SELECT COUNT(*) FROM items WHERE included=0").fetchone()[0]
        return int(inc), int(exc)

    def mark_uploaded(self, content_key: str, custom_emoji_id: str | None) -> None:
        self.db.execute(
            "UPDATE items SET uploaded=1, custom_emoji_id=? WHERE content_key=?",
            (custom_emoji_id, content_key),
        )
        self.db.commit()

    def get(self, content_key: str) -> Item | None:
        row = self.db.execute(
            "SELECT * FROM items WHERE content_key=?", (content_key,)
        ).fetchone()
        return _row_to_item(row) if row else None

    def merge_labels(self, content_key: str, *, emojis: list[str] | None = None,
                     keywords: list[str] | None = None, source: str | None = None,
                     file_unique_id: str | None = None) -> None:
        """Merge extra emoji/keywords/source onto an existing item (no new file).

        Used when a Telegram ``file_unique_id`` was already ingested: we skip the
        download and just record the additional label/source association.
        """
        if self.get(content_key) is None:
            return
        self._merge(content_key, emojis or [], keywords or [], source)
        if file_unique_id:
            self._record_seen(file_unique_id, content_key)
        self.db.commit()

    # ----- stats --------------------------------------------------------- #
    def stats(self) -> dict[str, dict[str, int]]:
        """Per-format {total, uploaded, excluded, pending} counts.

        ``pending`` uses the SAME predicate as :meth:`pending` -- not uploaded
        AND included. Counting it as ``total - uploaded`` reported items the
        user had deliberately excluded as still waiting to publish, so the
        number never reached zero.
        """
        out: dict[str, dict[str, int]] = {}
        for r in self.db.execute(
            "SELECT format, COUNT(*) n, SUM(uploaded) up, "
            "SUM(CASE WHEN included=0 THEN 1 ELSE 0 END) ex, "
            "SUM(CASE WHEN uploaded=0 AND included=1 THEN 1 ELSE 0 END) pend "
            "FROM items GROUP BY format"
        ):
            out[r["format"]] = {
                "total": int(r["n"]),
                "uploaded": int(r["up"] or 0),
                "excluded": int(r["ex"] or 0),
                "pending": int(r["pend"] or 0),
            }
        return out


def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
    """Order-preserving union of two string lists."""
    seen = set(base)
    out = list(base)
    for x in extra:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _row_to_item(r: sqlite3.Row) -> Item:
    return Item(
        content_key=r["content_key"], fmt=r["format"], file_path=r["file_path"],
        emojis=json.loads(r["emojis"]), keywords=json.loads(r["keywords"]),
        sources=json.loads(r["sources"]),
        phash=_phash_from_db(r["phash"]),
        custom_emoji_id=r["custom_emoji_id"], uploaded=bool(r["uploaded"]),
        included=bool(r["included"] if "included" in r.keys() else 1),
    )
