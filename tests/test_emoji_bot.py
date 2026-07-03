"""Tests for emoji_bot pure helpers (entity extraction + reply building)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import emoji_bot as b  # noqa: E402


def _msg(*cids):
    return {"entities": [{"type": "custom_emoji", "offset": i, "length": 2,
                          "custom_emoji_id": c} for i, c in enumerate(cids)],
            "text": "x" * (2 * len(cids))}


class TestExtract(unittest.TestCase):
    def test_none(self):
        self.assertEqual(b.extract_custom_emoji_ids({"text": "hi"}), [])

    def test_order_and_dedup(self):
        m = _msg("111", "222", "111", "333")
        self.assertEqual(b.extract_custom_emoji_ids(m), ["111", "222", "333"])

    def test_caption_entities(self):
        m = {"caption_entities": [{"type": "custom_emoji", "custom_emoji_id": "9"}]}
        self.assertEqual(b.extract_custom_emoji_ids(m), ["9"])


class TestBuildMessages(unittest.TestCase):
    def test_empty(self):
        msgs = b.build_messages([])
        self.assertEqual(len(msgs), 1)
        self.assertIn("No premium", msgs[0])

    def test_single_has_both_formats_and_copyable_code(self):
        msgs = b.build_messages(["5899"], {"5899": "👋"})
        self.assertEqual(len(msgs), 1)
        text = msgs[0]
        # Format 1: emoji + <code>id</code> ; Format 2: <code> block with the id.
        self.assertIn("👋 <code>5899</code>", text)
        self.assertIn("<code>5899</code>", text)
        self.assertEqual(text.count("<blockquote expandable>"), 2)

    def test_many_single_message_copy_all_block(self):
        ids = [str(1000 + i) for i in range(5)]
        msgs = b.build_messages(ids)
        self.assertEqual(len(msgs), 1)
        text = msgs[0]
        # Format 2 contains every id joined by newlines inside one <code> block.
        self.assertIn("<code>" + "\n".join(ids) + "</code>", text)
        # Format 1 lists each id in its own <code>.
        for cid in ids:
            self.assertIn(f"<code>{cid}</code>", text)

    def test_large_list_splits_into_multiple_messages(self):
        ids = [str(10**18 + i) for i in range(400)]  # 19-digit ids
        msgs = b.build_messages(ids)
        self.assertGreater(len(msgs), 1)          # had to split
        for m in msgs:
            self.assertLessEqual(len(m), 4096)    # each within Telegram's limit
        self.assertIn("part 1/", msgs[0])
        # Every id appears exactly once across all messages' Format 2 blocks.
        joined = "\n".join(msgs)
        for cid in ids:
            self.assertEqual(joined.count(f"<code>{cid}</code>"), 1)

    def test_unknown_label_uses_bullet(self):
        msgs = b.build_messages(["777"], {})
        self.assertIn("\u2022 <code>777</code>", msgs[0])


if __name__ == "__main__":
    unittest.main()
