"""Tests for emojikit.catalog: the duplicate-proof content-addressed catalog."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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
