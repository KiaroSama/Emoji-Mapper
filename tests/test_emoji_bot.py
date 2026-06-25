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


class TestBuildReply(unittest.TestCase):
    def test_empty(self):
        text, kb = b.build_reply([])
        self.assertIn("No premium", text)
        self.assertEqual(kb["inline_keyboard"], [])

    def test_single_button_copies_id(self):
        text, kb = b.build_reply(["5899"])
        self.assertIn("5899", text)
        btn = kb["inline_keyboard"][0][0]
        self.assertEqual(btn["copy_text"]["text"], "5899")

    def test_many_copy_all_fits(self):
        ids = [str(1000 + i) for i in range(5)]
        text, kb = b.build_reply(ids)
        # last row is a single "Copy all" button copying every id
        last = kb["inline_keyboard"][-1][0]
        self.assertEqual(last["copy_text"]["text"], "\n".join(ids))

    def test_many_copyall_chunked_under_limit(self):
        ids = [str(10**18 + i) for i in range(30)]  # 19-digit ids, >256 chars total
        text, kb = b.build_reply(ids)
        copyall = [r[0] for r in kb["inline_keyboard"]
                   if r and r[0]["text"].startswith("📋")]
        self.assertGreater(len(copyall), 1)  # split into chunks
        for btn in copyall:
            self.assertLessEqual(len(btn["copy_text"]["text"]), b.COPY_MAX)


if __name__ == "__main__":
    unittest.main()
