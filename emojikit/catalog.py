"""Content-addressed emoji catalog -- the duplicate-proof core.

The catalog is a small SQLite database that stores every prepared emoji exactly
once, keyed by a normalized *content hash* (see :func:`emojikit.identity.content_key`).
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

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import identity

log = logging.getLogger("emojikit.catalog")

SCHEMA_VERSION = 1
# Near-duplicate (perceptual) merging is OFF by default: faithfully copying a
# pack must keep visually-similar-but-DISTINCT emoji. Dedup then relies on exact
# content (normalized pixels) + file_unique_id only. Set a >=0 Hamming threshold
# (e.g. via --phash-threshold) to opt in to merging near-identical images.
DEFAULT_PHASH_THRESHOLD = -1
# A dHash is 64 bits, so 64 is the largest distance two hashes can have.
PHASH_BITS = 64
# ...which is exactly why 64 must NOT be accepted: `hamming(a, b) <= 64` holds
# for every pair, so that threshold merges every same-format item into one
# emoji. The whole range near 64 is just as broken, only less obviously: each
# dHash bit is an independent "is this pixel brighter than the next" comparison,
# so two UNRELATED images already differ in ~32 bits on average. A quarter of
# the hash is the conservative ceiling -- two random hashes land within 16 bits
# of each other with probability ~1e-5, so merging still means "the same
# picture, recompressed" and never "some other picture".
PHASH_MAX_THRESHOLD = PHASH_BITS // 4


def check_phash_threshold(value: int) -> int:
    """Validate a near-duplicate threshold: -1 disables merging, else 0..16."""
    value = int(value)
    if value != -1 and not 0 <= value <= PHASH_MAX_THRESHOLD:
        raise ValueError(
            f"phash threshold {value} is out of range: use -1 to disable "
            f"near-duplicate merging, or 0..{PHASH_MAX_THRESHOLD}. A dHash is "
            f"{PHASH_BITS} bits and two unrelated images already differ in "
            f"about {PHASH_BITS // 2} of them, so a larger threshold merges "
            f"emoji that have nothing in common.")
    return value


def phash_threshold_arg(raw: str) -> int:
    """argparse ``type=`` for --phash-threshold.

    Validating inside Catalog() alone is too late for a CLI: the ValueError is
    raised well after argparse has finished, so a bad value exits with a stack
    trace instead of the standard usage error.
    """
    try:
        return check_phash_threshold(int(raw))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
        # Validate before touching the database: an out-of-range threshold
        # silently merges unrelated emoji, and every caller routes through here.
        self.phash_threshold = check_phash_threshold(phash_threshold)
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            # add() / mark_uploaded() / record_file_unique_id() commit per row,
            # and the default rollback journal at synchronous=FULL makes each of
            # those several fsyncs. WAL at NORMAL can lose only the last
            # transaction if the OS itself dies -- it can never corrupt the
            # database or tear a committed row.
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        except Exception:
            # Otherwise a failed migration (routinely "database is locked",
            # since the panel builds a Catalog per request while
            # build_collection holds the file) leaks this connection: nobody
            # holds the half-built object, so nobody can close it.
            self.db.close()
            raise

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
            -- Publication state is per pack family (``base``), not per item.
            -- A single items.uploaded flag meant that publishing a catalog to
            -- one base marked its items done everywhere, so the SAME catalog
            -- could never be published to a second base, and a deleted pack
            -- could not be rebuilt without hand-editing the database.
            CREATE TABLE IF NOT EXISTS publications (
                base            TEXT NOT NULL,
                content_key     TEXT NOT NULL,
                set_name        TEXT,
                custom_emoji_id TEXT,
                uploaded_utc    TEXT NOT NULL,
                PRIMARY KEY (base, content_key)
            );
            CREATE INDEX IF NOT EXISTS idx_pub_base ON publications(base);
            """
        )
        self._migrate_publications()
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
            if identity.hamming(_phash_from_db(r["phash"]), phash) <= self.phash_threshold:
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
        cur_e = json.loads(row["emojis"])
        cur_k = json.loads(row["keywords"])
        cur_s = json.loads(row["sources"])
        merged_e = _merge_unique(cur_e, emojis)
        merged_k = _merge_unique(cur_k, keywords)
        merged_s = _merge_unique(cur_s, [source] if source else [])
        self.db.execute(
            "UPDATE items SET emojis=?, keywords=?, sources=? WHERE content_key=?",
            (json.dumps(merged_e), json.dumps(merged_k), json.dumps(merged_s), key),
        )

    # ----- publishing ---------------------------------------------------- #
    # ----- publication records (per pack family) -------------------------- #
    LEGACY_BASE = "__legacy__"

    def _migrate_publications(self) -> None:
        """Move pre-publications upload state into the new table, once.

        Old databases only recorded "uploaded" globally and did not record WHICH
        base it went to, so those rows land under LEGACY_BASE and are adopted by
        the first base that publishes (see :meth:`adopt_legacy_publication`).
        """
        if self.get_meta("publications_migrated") == "1":
            return
        rows = self.db.execute(
            "SELECT content_key, custom_emoji_id FROM items WHERE uploaded=1"
        ).fetchall()
        for r in rows:
            self.db.execute(
                "INSERT OR IGNORE INTO publications"
                "(base, content_key, set_name, custom_emoji_id, uploaded_utc) "
                "VALUES(?,?,?,?,?)",
                (self.LEGACY_BASE, r["content_key"], None,
                 r["custom_emoji_id"], _now()),
            )
        if rows:
            log.info("migrated %d uploaded item(s) into publication records",
                     len(rows))
        self.set_meta("publications_migrated", "1")
        self.db.commit()

    def adopt_legacy_publication(self, base: str) -> int:
        """Claim un-attributed legacy upload state for ``base``. Returns count."""
        if self.db.execute("SELECT 1 FROM publications WHERE base=? LIMIT 1",
                           (base,)).fetchone():
            return 0
        n = self.db.execute(
            "UPDATE publications SET base=? WHERE base=?",
            (base, self.LEGACY_BASE)).rowcount
        if n:
            log.info("adopted %d legacy publication record(s) into base %s", n, base)
            self.db.commit()
        return n

    def publication_bases(self) -> list[str]:
        return [r["base"] for r in self.db.execute(
            "SELECT DISTINCT base FROM publications ORDER BY base")]

    def is_published(self, base: str, content_key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM publications WHERE base=? AND content_key=?",
            (base, content_key)).fetchone() is not None

    def published_keys(self) -> set[str]:
        """Every content_key live in SOME pack, across every base.

        Deliberately not ``published_set_names()``: that one drops rows whose
        ``set_name`` is NULL, and an item whose set is unrecorded is still
        published. The question here is "is this already live", so a row is
        enough -- reading the set name would turn "I do not know where" into
        "not published" and offer a live emoji up for republishing.
        """
        return {r[0] for r in self.db.execute(
            "SELECT DISTINCT content_key FROM publications")}

    def published_set_names(self) -> dict[str, str]:
        """content_key -> the set it was published into, across every base.

        One query rather than a lookup per item: the panel asks this for the
        whole catalog on every page load.
        """
        return {k: n for k, n in self.db.execute(
            "SELECT content_key, set_name FROM publications WHERE set_name IS NOT NULL")}

    def custom_emoji_id_for(self, base: str, content_key: str) -> str | None:
        row = self.db.execute(
            "SELECT custom_emoji_id FROM publications WHERE base=? AND content_key=?",
            (base, content_key)).fetchone()
        return row["custom_emoji_id"] if row else None

    def forget_publication(self, base: str) -> int:
        """Drop every record for a pack family, so it can be published again.

        Needed when the live sets were deleted or rebuilt: without this the
        catalog insisted the items were already uploaded.
        """
        n = self.db.execute("DELETE FROM publications WHERE base=?", (base,)).rowcount
        self.db.commit()
        log.info("cleared %d publication record(s) for base %s", n, base)
        return n

    def pending(self, fmt: str | None = None, *, base: str | None = None) -> list[Item]:
        """Included items still to publish, in deterministic (frozen) order.

        With ``base`` the answer is scoped to that pack family, so the same
        catalog can be published to several bases independently. Without it the
        legacy global ``uploaded`` flag is used (kept for the ingest commands'
        progress counts, which are not tied to a pack family).
        """
        where = ["included=1"]
        params: list = []
        if base is None:
            where.append("uploaded=0")
        else:
            where.append("content_key NOT IN "
                         "(SELECT content_key FROM publications WHERE base=?)")
            params.append(base)
        if fmt:
            where.append("format=?")
            params.append(fmt)
        rows = self.db.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} "
            f"ORDER BY position, content_key", params).fetchall()
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
        if not ordered_keys:
            return 0        # otherwise the offset below rewrites every row by 0
        # Push EVERY item back first, then overwrite the listed ones. The old
        # form excluded the listed keys with a ``NOT IN (?,?,...)`` holding one
        # SQL parameter per key, which raises "too many SQL variables" past
        # SQLite's ~32 766 limit -- a hard ceiling on catalog size reachable
        # from a single drag in the panel. Offsetting everything is equivalent
        # (a listed key's pushed position is immediately replaced by its index)
        # and takes no parameters at all.
        self.db.execute("UPDATE items SET position = position + ?",
                        (len(ordered_keys),))
        cur = self.db.executemany(
            "UPDATE items SET position=? WHERE content_key=?",
            list(enumerate(ordered_keys)))
        self.db.commit()
        return cur.rowcount

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

    def mark_uploaded(self, content_key: str, custom_emoji_id: str | None, *,
                      base: str | None = None, set_name: str | None = None) -> None:
        """Record that an item is live, for a specific pack family.

        ``base`` scopes the record so publishing to one family never marks the
        item done for another. The legacy per-item columns are still mirrored,
        because ingest commands report progress from them.
        """
        self.db.execute(
            "UPDATE items SET uploaded=1, custom_emoji_id=? WHERE content_key=?",
            (custom_emoji_id, content_key),
        )
        if base:
            self.db.execute(
                "INSERT INTO publications"
                "(base, content_key, set_name, custom_emoji_id, uploaded_utc) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(base, content_key) DO UPDATE SET "
                "  set_name=COALESCE(excluded.set_name, set_name),"
                "  custom_emoji_id=COALESCE(excluded.custom_emoji_id, custom_emoji_id)",
                (base, content_key, set_name, custom_emoji_id, _now()),
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
