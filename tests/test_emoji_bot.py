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

    def test_quote_entities_are_scanned(self):
        # Per the Bot API, when a reply quotes part of the original message,
        # only bold/italic/.../custom_emoji entities survive inside
        # message.quote (a TextQuote). Multiple premium emoji in a manually
        # quoted excerpt must ALL be extracted, not just the first one.
        m = {
            "text": "reply text",
            "quote": {
                "text": "wave warn stop",
                "position": 0,
                "entities": [
                    {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "111"},
                    {"type": "custom_emoji", "offset": 5, "length": 2, "custom_emoji_id": "222"},
                    {"type": "custom_emoji", "offset": 10, "length": 2, "custom_emoji_id": "333"},
                ],
            },
        }
        self.assertEqual(b.extract_custom_emoji_ids(m), ["111", "222", "333"])

    def test_external_reply_quote_entities_are_scanned(self):
        # Same TextQuote mechanism, but for a reply to a message from another
        # chat (external_reply.quote instead of quote).
        m = {
            "external_reply": {
                "quote": {
                    "text": "aa bb",
                    "entities": [
                        {"type": "custom_emoji", "custom_emoji_id": "444"},
                        {"type": "custom_emoji", "custom_emoji_id": "555"},
                    ],
                }
            }
        }
        self.assertEqual(b.extract_custom_emoji_ids(m), ["444", "555"])

    def test_quote_plus_own_entities_dedup_and_order(self):
        # Repeated real emoji across quote + own entities must be listed once,
        # in first-seen order.
        m = {
            "entities": [{"type": "custom_emoji", "custom_emoji_id": "111"}],
            "quote": {"entities": [
                {"type": "custom_emoji", "custom_emoji_id": "222"},
                {"type": "custom_emoji", "custom_emoji_id": "111"},  # duplicate
            ]},
        }
        self.assertEqual(b.extract_custom_emoji_ids(m), ["111", "222"])


class TestBuildPayloads(unittest.TestCase):
    def test_empty(self):
        payloads = b.build_payloads([])
        self.assertEqual(len(payloads), 1)
        text, kb = payloads[0]
        self.assertIn("No premium", text)
        self.assertEqual(kb["inline_keyboard"], [])

    def test_rich_renders_real_premium_emoji(self):
        # Format 1 must render the ACTUAL premium emoji via <tg-emoji>.
        text, _ = b.build_payloads(["5899"], {"5899": "👋"}, rich=True)[0]
        self.assertIn('<tg-emoji emoji-id="5899">👋</tg-emoji> <code>5899</code>', text)

    def test_single_collapsed_quote_only(self):
        text, _ = b.build_payloads(["1", "2"], {})[0]
        # Only ONE collapsed quote now (Format 2 was removed); no <pre>.
        self.assertEqual(text.count("<blockquote expandable>"), 1)
        self.assertNotIn("<pre>", text)
        self.assertNotIn("IDs only", text)

    def test_copy_all_button_copies_every_id(self):
        ids = [str(1000 + i) for i in range(5)]
        text, kb = b.build_payloads(ids)[0]
        btn = kb["inline_keyboard"][-1][0]
        self.assertEqual(btn["copy_text"]["text"], "\n".join(ids))
        # Each id appears once as a <code> in the single quote.
        for cid in ids:
            self.assertEqual(text.count(f"<code>{cid}</code>"), 1)

    def test_copy_buttons_chunked_under_limit(self):
        ids = [str(10**18 + i) for i in range(30)]  # 19-digit ids > 256 chars
        _, kb = b.build_payloads(ids)[0]
        copy_btns = [r[0] for r in kb["inline_keyboard"] if r and "copy_text" in r[0]]
        self.assertGreater(len(copy_btns), 1)
        for btn in copy_btns:
            self.assertLessEqual(len(btn["copy_text"]["text"]), b.COPY_MAX)

    def test_plain_uses_fallback_char_only(self):
        text, _ = b.build_payloads(["5899"], {"5899": "👋"}, rich=False)[0]
        self.assertIn("👋 <code>5899</code>", text)
        self.assertNotIn("tg-emoji", text)

    def test_rich_default_and_missing_label_uses_star(self):
        text, _ = b.build_payloads(["777"], {})[0]
        self.assertIn('<tg-emoji emoji-id="777">\u2b50</tg-emoji>', text)

    def test_large_list_splits_into_multiple_messages(self):
        ids = [str(10**18 + i) for i in range(400)]  # 19-digit ids
        payloads = b.build_payloads(ids, rich=True)
        self.assertGreater(len(payloads), 1)              # had to split
        for text, _ in payloads:
            self.assertLessEqual(len(text), 4096)          # within Telegram's limit
        self.assertIn("part 1/", payloads[0][0])
        # Every id appears exactly once across all messages' quotes.
        joined = "\n".join(t for t, _ in payloads)
        for cid in ids:
            self.assertEqual(joined.count(f"<code>{cid}</code>"), 1)

    def test_rich_and_plain_batch_alignment(self):
        # Rich and plain must split into the SAME batches so a per-message
        # fallback (rich[i] -> plain[i]) always covers the same ids.
        ids = [str(10**18 + i) for i in range(400)]
        rich = b.build_payloads(ids, rich=True)
        plain = b.build_payloads(ids, rich=False)
        self.assertEqual(len(rich), len(plain))


if __name__ == "__main__":
    unittest.main()
