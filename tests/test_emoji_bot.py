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

    def test_rich_renders_real_premium_emoji(self):
        # Format 1 must render the ACTUAL premium emoji via <tg-emoji>.
        msgs = b.build_messages(["5899"], {"5899": "👋"}, rich=True)
        text = msgs[0]
        self.assertIn('<tg-emoji emoji-id="5899">👋</tg-emoji> <code>5899</code>', text)

    def test_format1_is_collapsed_expandable_quote(self):
        # Format 1 is a collapsed (expandable) blockquote, as requested.
        text = b.build_messages(["1", "2"], {})[0]
        self.assertIn("<blockquote expandable>", text)

    def test_format2_is_pre_block_with_all_ids(self):
        ids = [str(1000 + i) for i in range(5)]
        text = b.build_messages(ids)[0]
        # Format 2 = one <pre> block (Telegram Copy button) holding every id.
        self.assertIn("<pre>" + "\n".join(ids) + "</pre>", text)

    def test_both_formats_have_same_id_count(self):
        ids = ["5472055112702629499", "6260129328881732024",
               "5215313353706057331", "5215670591905869044"]
        text = b.build_messages(ids)[0]
        pre = text.split("<pre>")[1].split("</pre>")[0]
        # Format 1: one <code>id</code> per id; Format 2: id present in <pre>.
        for cid in ids:
            self.assertEqual(text.count(f"<code>{cid}</code>"), 1)
            self.assertIn(cid, pre)
        # Same number of ids in each section (4 and 4).
        self.assertEqual(len([c for c in ids if f"<code>{c}</code>" in text]), 4)
        self.assertEqual(len(pre.strip().splitlines()), 4)

    def test_plain_uses_fallback_char_only(self):
        msgs = b.build_messages(["5899"], {"5899": "👋"}, rich=False)
        text = msgs[0]
        self.assertIn("👋 <code>5899</code>", text)
        self.assertNotIn("tg-emoji", text)

    def test_rich_default_and_missing_label_uses_star(self):
        msgs = b.build_messages(["777"], {})  # rich defaults True, no label
        self.assertIn('<tg-emoji emoji-id="777">\u2b50</tg-emoji>', msgs[0])

    def test_large_list_splits_into_multiple_messages(self):
        ids = [str(10**18 + i) for i in range(400)]  # 19-digit ids
        msgs = b.build_messages(ids, rich=True)
        self.assertGreater(len(msgs), 1)          # had to split
        for m in msgs:
            self.assertLessEqual(len(m), 4096)    # each within Telegram's limit
        self.assertIn("part 1/", msgs[0])
        # Every id appears exactly once across all messages' Format 2 blocks.
        joined = "\n".join(msgs)
        for cid in ids:
            self.assertEqual(joined.count(f"<code>{cid}</code>"), 1)

    def test_rich_and_plain_batch_alignment(self):
        # Rich and plain must split into the SAME batches so a per-message
        # fallback (rich[i] -> plain[i]) always covers the same ids.
        ids = [str(10**18 + i) for i in range(400)]
        rich = b.build_messages(ids, rich=True)
        plain = b.build_messages(ids, rich=False)
        self.assertEqual(len(rich), len(plain))


if __name__ == "__main__":
    unittest.main()
