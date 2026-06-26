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
        self.db.execute(
            "INSERT OR REPLACE INTO seen_files(file_unique_id, content_key) VALUES(?, ?)",
            (fuid, content_key),
        )

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
            return canonical, False

        self.db.execute(
            "INSERT INTO items(content_key, format, file_path, emojis, keywords, "
            "sources, phash, uploaded, created_utc) VALUES(?,?,?,?,?,?,?,0,?)",
            (content_key, fmt, str(file_path), json.dumps(emojis),
             json.dumps(keywords), json.dumps([source] if source else []),
             _phash_to_db(phash), _now()),
        )
        if file_unique_id:
            self._record_seen(file_unique_id, content_key)
        self.db.commit()
        log.debug("new %s item %s (%s)", fmt, content_key, file_path)
        return content_key, True

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
        """Items not yet uploaded, in deterministic (frozen) order."""
        if fmt:
            rows = self.db.execute(
                "SELECT * FROM items WHERE uploaded=0 AND format=? ORDER BY content_key",
                (fmt,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM items WHERE uploaded=0 ORDER BY format, content_key"
            ).fetchall()
        return [_row_to_item(r) for r in rows]

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
        """Per-format {total, uploaded, pending} counts."""
        out: dict[str, dict[str, int]] = {}
        for r in self.db.execute(
            "SELECT format, COUNT(*) n, SUM(uploaded) up FROM items GROUP BY format"
        ):
            total = int(r["n"]); up = int(r["up"] or 0)
            out[r["format"]] = {"total": total, "uploaded": up, "pending": total - up}
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
    )
