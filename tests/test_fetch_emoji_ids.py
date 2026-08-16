"""Tests for the premium-id extraction and de-duplication in fetch_emoji_ids.

These lock down the duplicate-detection behaviour so future inventory-format
changes cannot silently reintroduce the bugs we hit manually:
  * example/prose mentions must never be treated as real entries;
  * real entries with a trailing label/emoji must still be captured;
  * cross-file and within-file duplicates must be detected and collapsed to one.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetch_emoji_ids import (
    collect_ids,
    extract_real_ids,
    within_file_duplicates,
)

REAL_A = "5334673106202010226"
REAL_B = "6260129328881732024"
REAL_C = "5215313353706057331"
EXAMPLE = "5472055112702629499"


class ExtractRealIds(unittest.TestCase):
    def test_bare_entry_lines(self):
        text = f"premium-id:{REAL_A}\npremium-id:{REAL_B}\n"
        self.assertEqual(extract_real_ids(text), [REAL_A, REAL_B])

    def test_example_prose_line_is_ignored(self):
        text = (
            f"- Set a numeric custom_emoji_id (e.g. premium-id: {EXAMPLE}) OR a full\n"
            f"premium-id:{REAL_A}\n"
        )
        # Only the real entry is returned; the example mention is skipped.
        self.assertEqual(extract_real_ids(text), [REAL_A])

    def test_trailing_label_and_emoji_still_captured(self):
        # Future format: id followed by a label/emoji on the same line.
        text = f"premium-id:{REAL_A} 👋 waving hand\n- premium-id:{REAL_B} | fire\n"
        self.assertEqual(extract_real_ids(text), [REAL_A, REAL_B])

    def test_bullet_and_spacing_variants(self):
        text = (
            f"  - premium-id:{REAL_A}\n"
            f"* PREMIUM-ID : {REAL_B}\n"
            f"premium-id:  {REAL_C}\n"
        )
        self.assertEqual(extract_real_ids(text), [REAL_A, REAL_B, REAL_C])

    def test_bare_number_list_fallback(self):
        text = f"{REAL_A}\n{REAL_B}\n   {REAL_C}   \n"
        self.assertEqual(extract_real_ids(text), [REAL_A, REAL_B, REAL_C])

    def test_short_or_nonnumeric_noise_ignored(self):
        text = "premium-id:123\nrandom prose 5334673106202010226 in text\nhello\n"
        # 123 is too short; the mid-prose number is not a real entry line.
        self.assertEqual(extract_real_ids(text), [])

    def test_overlong_number_not_truncated(self):
        # 30 digits: must not be partially matched to a 25-digit id.
        text = f"premium-id:{'9' * 30}\npremium-id:{REAL_A}\n"
        self.assertEqual(extract_real_ids(text), [REAL_A])


class WithinFileDuplicates(unittest.TestCase):
    def test_detects_true_within_file_duplicate(self):
        text = f"premium-id:{REAL_A}\npremium-id:{REAL_B}\npremium-id:{REAL_A}\n"
        self.assertEqual(within_file_duplicates(text), {REAL_A: 2})

    def test_example_line_is_not_a_duplicate(self):
        # The bug we hit: example mention + one real entry must NOT count as dup.
        text = (
            f"- Set a numeric custom_emoji_id (e.g. premium-id: {EXAMPLE}) OR a full\n"
            f"premium-id:{EXAMPLE}\n"
        )
        self.assertEqual(within_file_duplicates(text), {})


class CollectIdsDedup(unittest.TestCase):
    def _write(self, tmp, name, text):
        p = Path(tmp) / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_cross_file_dedup_preserves_first_seen_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = self._write(tmp, "a.md", f"premium-id:{REAL_A}\npremium-id:{REAL_B}\n")
            f2 = self._write(tmp, "b.md", f"premium-id:{REAL_B}\npremium-id:{REAL_C}\n")
            ids = collect_ids([f1, f2], [])
            self.assertEqual(ids, [REAL_A, REAL_B, REAL_C])  # B appears once

    def test_within_and_cross_file_and_inline_all_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = self._write(tmp, "a.md",
                             f"premium-id:{REAL_A}\npremium-id:{REAL_A}\n")  # within-file dup
            ids = collect_ids([f1], [REAL_A, REAL_B])  # inline repeats A, adds B
            self.assertEqual(ids, [REAL_A, REAL_B])

    def test_example_ids_never_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = self._write(
                tmp, "a.md",
                f"- e.g. premium-id: {EXAMPLE} in the header\npremium-id:{REAL_A}\n")
            ids = collect_ids([f1], [])
            self.assertEqual(ids, [REAL_A])
            self.assertNotIn(EXAMPLE, ids)


if __name__ == "__main__":
    unittest.main()
