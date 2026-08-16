"""Tests for emojikit.catalog: the duplicate-proof content-addressed catalog."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit.catalog import Catalog  # noqa: E402


class TestCatalogIntegrity(unittest.TestCase):
    """Regressions for the catalog's silent-corruption paths."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog(self.tmp / "catalog.db", phash_threshold=-1)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _media(self, name: str, data: bytes = b"x") -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_fuid_is_not_silently_reassigned(self):
        """One Telegram file maps to one item; a conflict must not overwrite."""
        self.cat.add(content_key="s:one", fmt="static",
                     file_path=self._media("a.png"), file_unique_id="FUID1")
        self.cat.add(content_key="s:two", fmt="static",
                     file_path=self._media("b.png"), file_unique_id="FUID1")
        self.assertEqual(self.cat.seen_file_unique_id("FUID1"), "s:one",
                         "the first mapping must win")

    def test_same_fuid_same_key_is_idempotent(self):
        self.cat.add(content_key="s:one", fmt="static",
                     file_path=self._media("a.png"), file_unique_id="FUID1")
        self.cat.record_file_unique_id("FUID1", "s:one")
        self.assertEqual(self.cat.seen_file_unique_id("FUID1"), "s:one")

    def test_merged_duplicate_leaves_no_orphan_file(self):
        """The losing copy of a dedup merge must not stay on disk."""
        first = self._media("first.png")
        self.cat.add(content_key="s:same", fmt="static", file_path=first)
        second = self._media("second.png")
        key, is_new = self.cat.add(content_key="s:same", fmt="static",
                                   file_path=second)
        self.assertFalse(is_new)
        self.assertEqual(key, "s:same")
        self.assertTrue(first.is_file(), "the canonical file must survive")
        self.assertFalse(second.is_file(), "the redundant copy must be removed")

    def test_readding_the_canonical_file_does_not_delete_it(self):
        only = self._media("only.png")
        self.cat.add(content_key="s:same", fmt="static", file_path=only)
        self.cat.add(content_key="s:same", fmt="static", file_path=only)
        self.assertTrue(only.is_file())

    def test_same_catalog_publishes_to_two_bases(self):
        """The defect: publishing to one base marked items done everywhere."""
        for i in range(3):
            self.cat.add(content_key=f"s:k{i}", fmt="static",
                         file_path=self._media(f"m{i}.png", bytes([i])))
        self.assertEqual(len(self.cat.pending(base="one")), 3)

        for it in self.cat.pending(base="one"):
            self.cat.mark_uploaded(it.content_key, f"cid_{it.content_key}",
                                   base="one", set_name="one1")
        self.assertEqual(len(self.cat.pending(base="one")), 0)
        self.assertEqual(len(self.cat.pending(base="two")), 3,
                         "a second pack family must still see every item")

    def test_publication_records_are_per_base(self):
        self.cat.add(content_key="s:k", fmt="static",
                     file_path=self._media("m.png"))
        self.cat.mark_uploaded("s:k", "cid_one", base="one", set_name="one1")
        self.assertTrue(self.cat.is_published("one", "s:k"))
        self.assertFalse(self.cat.is_published("two", "s:k"))
        self.assertEqual(self.cat.custom_emoji_id_for("one", "s:k"), "cid_one")
        self.assertIsNone(self.cat.custom_emoji_id_for("two", "s:k"))

    def test_forgetting_a_deleted_pack_allows_republishing(self):
        self.cat.add(content_key="s:k", fmt="static",
                     file_path=self._media("m.png"))
        self.cat.mark_uploaded("s:k", "cid", base="one", set_name="one1")
        self.assertEqual(len(self.cat.pending(base="one")), 0)
        self.assertEqual(self.cat.forget_publication("one"), 1)
        self.assertEqual(len(self.cat.pending(base="one")), 1,
                         "a deleted pack family must be publishable again")

    def test_legacy_upload_state_is_adopted_once(self):
        self.cat.add(content_key="s:k", fmt="static",
                     file_path=self._media("m.png"))
        # Simulate a pre-publications database: uploaded flag, no record.
        self.cat.db.execute("UPDATE items SET uploaded=1 WHERE content_key='s:k'")
        self.cat.db.execute("DELETE FROM publications")
        self.cat.set_meta("publications_migrated", "0")
        self.cat._migrate_publications()
        self.assertEqual(self.cat.publication_bases(), [Catalog.LEGACY_BASE])

        self.assertEqual(self.cat.adopt_legacy_publication("one"), 1)
        self.assertTrue(self.cat.is_published("one", "s:k"))
        # A second base must NOT also claim it.
        self.assertEqual(self.cat.adopt_legacy_publication("two"), 0)
        self.assertFalse(self.cat.is_published("two", "s:k"))

    def test_excluded_items_are_not_counted_as_pending(self):
        for i in range(3):
            self.cat.add(content_key=f"s:k{i}", fmt="static",
                         file_path=self._media(f"m{i}.png", bytes([i])))
        self.cat.set_inclusion({"s:k0"})          # exclude one
        stats = self.cat.stats()["static"]
        pending_rows = [it for it in self.cat.pending() if it.fmt == "static"]
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["excluded"], 1)
        self.assertEqual(stats["pending"], len(pending_rows),
                         "stats must use the publisher's own predicate")
        self.assertEqual(stats["pending"], 2)


class TestCatalogDurabilityAndScale(unittest.TestCase):
    """The connection-level settings and the limits they have to survive."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog(self.tmp / "catalog.db", phash_threshold=-1)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _media(self, name: str, data: bytes = b"x") -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_wal_and_relaxed_sync(self):
        """Every add/mark_uploaded commits, so the journal mode is the cost."""
        self.assertEqual(
            self.cat.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(
            self.cat.db.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL

    def test_failed_init_does_not_leak_the_connection(self):
        """The panel builds a Catalog per POST; a locked migration leaked one
        open connection per failed request, with no owner left to close it."""
        opened = []
        real_connect = sqlite3.connect

        def spy(*a, **kw):
            con = real_connect(*a, **kw)
            opened.append(con)
            return con

        with mock.patch.object(sqlite3, "connect", spy), \
                mock.patch.object(Catalog, "_init_schema",
                                  side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                Catalog(self.tmp / "broken.db")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_set_order_survives_a_key_list_past_the_variable_limit(self):
        """The exclusion list used one SQL parameter per key, so a catalog
        larger than SQLite's ~32 766 variable ceiling could not be reordered."""
        self.cat.add(content_key="s:real", fmt="static",
                     file_path=self._media("r.png"))
        keys = ["s:real"] + [f"s:ghost{i}" for i in range(40_000)]
        self.assertEqual(self.cat.set_order(keys), 1, "only the real row exists")
        self.assertEqual(self.cat.all_items()[0].content_key, "s:real")

    def test_set_order_pushes_unlisted_items_after(self):
        for i in range(3):
            self.cat.add(content_key=f"s:k{i}", fmt="static",
                         file_path=self._media(f"m{i}.png", bytes([i])))
        self.assertEqual(self.cat.set_order(["s:k2"]), 1)
        self.assertEqual([it.content_key for it in self.cat.all_items()],
                         ["s:k2", "s:k0", "s:k1"],
                         "unlisted items keep their relative order, after")

    def test_empty_order_writes_nothing(self):
        """The offset-everything form would rewrite every row by zero."""
        for i in range(3):
            self.cat.add(content_key=f"s:k{i}", fmt="static",
                         file_path=self._media(f"m{i}.png", bytes([i])))
        before = self.cat.db.total_changes
        self.assertEqual(self.cat.set_order([]), 0)
        self.assertEqual(self.cat.db.total_changes, before,
                         "an empty order must not touch a single row")


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog(self.tmp / "catalog.db", phash_threshold=4)
        (self.tmp / "f.png").write_bytes(b"x")  # dummy media file

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _f(self):
        return self.tmp / "f.png"

    def test_add_new_and_exact_dedup(self):
        key, new = self.cat.add(content_key="s:abc", fmt="static", file_path=self._f(),
                                emojis=["😀"], keywords=["a"], source="p1")
        self.assertTrue(new)
        # Same content key again -> merged, not new.
        key2, new2 = self.cat.add(content_key="s:abc", fmt="static", file_path=self._f(),
                                  emojis=["🔥"], keywords=["b"], source="p2")
        self.assertFalse(new2)
        self.assertEqual(key, key2)
        item = self.cat.get("s:abc")
        self.assertEqual(set(item.emojis), {"😀", "🔥"})
        self.assertEqual(set(item.keywords), {"a", "b"})
        self.assertEqual(set(item.sources), {"p1", "p2"})

    def test_near_duplicate_merge_by_phash(self):
        self.cat.add(content_key="s:k1", fmt="static", file_path=self._f(),
                     emojis=["A"], phash=0b0000)
        # Different content key but phash within threshold (distance 2) -> merge.
        key, new = self.cat.add(content_key="s:k2", fmt="static", file_path=self._f(),
                                emojis=["B"], phash=0b0011)
        self.assertFalse(new)
        self.assertEqual(key, "s:k1")
        self.assertIsNone(self.cat.get("s:k2"))
        self.assertEqual(set(self.cat.get("s:k1").emojis), {"A", "B"})

    def test_phash_beyond_threshold_is_new(self):
        self.cat.add(content_key="s:k1", fmt="static", file_path=self._f(), phash=0)
        key, new = self.cat.add(content_key="s:k2", fmt="static", file_path=self._f(),
                                phash=0b11111111)  # distance 8 > 4
        self.assertTrue(new)
        self.assertEqual(key, "s:k2")

    def test_seen_file_unique_id_skips(self):
        self.cat.add(content_key="s:k1", fmt="static", file_path=self._f(),
                     file_unique_id="FUID1")
        self.assertEqual(self.cat.seen_file_unique_id("FUID1"), "s:k1")
        self.assertIsNone(self.cat.seen_file_unique_id("OTHER"))

    def test_pending_and_mark_uploaded(self):
        self.cat.add(content_key="s:a", fmt="static", file_path=self._f())
        self.cat.add(content_key="v:b", fmt="video", file_path=self._f())
        self.assertEqual(len(self.cat.pending()), 2)
        self.assertEqual(len(self.cat.pending("static")), 1)
        self.cat.mark_uploaded("s:a", "cid123")
        self.assertEqual(len(self.cat.pending("static")), 0)
        self.assertEqual(self.cat.get("s:a").custom_emoji_id, "cid123")
        self.assertTrue(self.cat.get("s:a").uploaded)

    def test_stats(self):
        self.cat.add(content_key="s:a", fmt="static", file_path=self._f())
        self.cat.add(content_key="s:b", fmt="static", file_path=self._f())
        self.cat.mark_uploaded("s:a", None)
        s = self.cat.stats()
        self.assertEqual(s["static"],
                         {"total": 2, "uploaded": 1, "excluded": 0, "pending": 1})

    def test_large_phash_64bit(self):
        # A full 64-bit dHash can exceed SQLite's signed-64-bit INTEGER max.
        big = (1 << 64) - 1  # all bits set
        self.cat.add(content_key="s:big", fmt="static", file_path=self._f(), phash=big)
        self.assertEqual(self.cat.get("s:big").phash, big)
        # near-dup lookup must still work with such values
        k, new = self.cat.add(content_key="s:big2", fmt="static",
                              file_path=self._f(), phash=big)
        self.assertFalse(new)  # identical phash -> merged onto s:big
        self.assertEqual(k, "s:big")

    def test_persistence_across_reopen(self):
        self.cat.add(content_key="s:a", fmt="static", file_path=self._f(),
                     file_unique_id="FX")
        self.cat.close()
        cat2 = Catalog(self.tmp / "catalog.db")
        try:
            self.assertIsNotNone(cat2.get("s:a"))
            self.assertEqual(cat2.seen_file_unique_id("FX"), "s:a")
        finally:
            cat2.close()


if __name__ == "__main__":
    unittest.main()
