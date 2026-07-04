"""Tests for the manual publish order (position column) used by the curate
panel's drag-and-drop and by build_collection's publish order."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit.catalog import Catalog  # noqa: E402


def _png(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(p, "PNG")


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "catalog.db"
        self.keys = []
        with Catalog(self.db) as cat:
            for i in range(5):
                img = Path(self.tmp.name) / f"m{i}.png"; _png(img)
                k = f"s:k{i:030d}"
                cat.add(content_key=k, fmt="static", file_path=img, keywords=[f"k{i}"])
                self.keys.append(k)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_order_is_insertion(self):
        with Catalog(self.db) as cat:
            got = [it.content_key for it in cat.all_items()]
        self.assertEqual(got, self.keys)

    def test_set_order_reorders(self):
        new = list(reversed(self.keys))
        with Catalog(self.db) as cat:
            cat.set_order(new)
        with Catalog(self.db) as cat:
            got = [it.content_key for it in cat.all_items()]
        self.assertEqual(got, new)

    def test_order_persists_across_reopen_and_new_items_go_last(self):
        new = [self.keys[2], self.keys[0], self.keys[1], self.keys[3], self.keys[4]]
        with Catalog(self.db) as cat:
            cat.set_order(new)
            img = Path(self.tmp.name) / "m5.png"; _png(img)
            cat.add(content_key="s:k99", fmt="static", file_path=img, keywords=["k99"])
        with Catalog(self.db) as cat:
            got = [it.content_key for it in cat.all_items()]
        self.assertEqual(got[:5], new)          # saved order preserved
        self.assertEqual(got[-1], "s:k99")      # newly added item goes last

    def test_meta_roundtrip(self):
        with Catalog(self.db) as cat:
            self.assertIsNone(cat.get_meta("order_seeded"))
            cat.set_meta("order_seeded", "1")
            self.assertEqual(cat.get_meta("order_seeded"), "1")


if __name__ == "__main__":
    unittest.main()
