"""Tests for emoji_bot pure helpers (entity extraction + reply building)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # Trailing newline so pasting the ids is followed by a blank line.
        self.assertEqual(btn["copy_text"]["text"], "\n".join(ids) + "\n")
        # Each id appears once as a <code> in the single quote.
        for cid in ids:
            self.assertEqual(text.count(f"<code>{cid}</code>"), 1)

    def test_50_ids_batched_by_message_limit_not_button_limit(self):
        # Batching follows the (larger) message-length limit, not the smaller
        # copy_text button limit, so as many ids as possible land per message
        # (e.g. 50 ids -> 2 messages, not 5). Each message's copy button(s)
        # together cover exactly that message's ids, in order, once each.
        ids = [str(10**18 + i) for i in range(50)]
        payloads = b.build_payloads(ids)
        self.assertLess(len(payloads), 5)  # far fewer messages than the old bug
        covered = []
        for text, kb in payloads:
            btn_rows = [r for r in kb["inline_keyboard"] if r and "copy_text" in r[0]]
            self.assertGreaterEqual(len(btn_rows), 1)
            msg_ids = []
            for row in btn_rows:
                piece = row[0]["copy_text"]["text"]
                self.assertLessEqual(len(piece), b.COPY_MAX)
                self.assertTrue(piece.endswith("\n"))  # trailing blank line
                msg_ids.extend(piece.rstrip("\n").split("\n"))
            for cid in msg_ids:
                self.assertIn(f"<code>{cid}</code>", text)  # buttons match the message
            covered.extend(msg_ids)
        self.assertEqual(covered, ids)  # every id covered, in order, exactly once

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


class TestCopyButtonLimit(unittest.TestCase):
    """CopyTextButton.text is capped at 256 chars by the Bot API.

    Packing a fixed 12 ids per button overflowed as soon as ids were long: the
    id parser accepts up to 25 digits, and 12 of those plus newlines is 312.
    """

    def _assert_covers(self, ids):
        kb = b._copy_keyboard(ids)["inline_keyboard"]
        copied = []
        for row in kb:
            text = row[0]["copy_text"]["text"]
            self.assertLessEqual(len(text), b.COPY_MAX,
                                 f"button text {len(text)} > {b.COPY_MAX}")
            copied.extend(text.strip().split("\n"))
        self.assertEqual(copied, ids, "every id exactly once, in order")
        return kb

    def test_short_ids_fit_one_button(self):
        kb = self._assert_covers(["111111111"] * 3)
        self.assertEqual(len(kb), 1)

    def test_nineteen_digit_ids(self):
        self._assert_covers([str(10**18 + i) for i in range(12)])

    def test_max_length_ids_are_split(self):
        ids = [str(9) * 25 for _ in range(12)]
        kb = self._assert_covers(ids)
        self.assertGreater(len(kb), 1, "must split rather than overflow")

    def test_many_max_length_ids(self):
        self._assert_covers([str(9) * 25 for _ in range(50)])

    def test_single_id(self):
        self._assert_covers(["111111111"])


class TestAccessControl(unittest.TestCase):
    """The bot is private: only listed user ids may use it."""

    def setUp(self):
        self.sent = []
        self.tg = mock.Mock()
        self.tg._call.side_effect = lambda m, **kw: self.sent.append((m, kw))

    def _msg(self, uid, text="hi", chat_type="private"):
        return {"message": {"message_id": 1, "text": text,
                            "from": {"id": uid},
                            "chat": {"id": uid, "type": chat_type}}}

    def _env(self, **kw):
        return mock.patch.dict(os.environ, kw, clear=False)

    def test_allowlist_defaults_to_the_owner(self):
        with self._env(PACK_OWNER_USER_ID="42", BOT_ALLOWED_USER_IDS=""):
            self.assertEqual(b.allowed_user_ids(), {42})

    def test_allowlist_parses_separators_and_ignores_junk(self):
        with self._env(BOT_ALLOWED_USER_IDS="1, 2 ;3, oops,"):
            self.assertEqual(b.allowed_user_ids(), {1, 2, 3})

    def test_empty_configuration_authorizes_nobody(self):
        """A misconfiguration must fail closed, not open the bot to everyone."""
        with self._env(PACK_OWNER_USER_ID="0", BOT_ALLOWED_USER_IDS=""):
            self.assertEqual(b.allowed_user_ids(), set())

    def test_authorized_user_is_served(self):
        b.handle_update(self.tg, 42, self._msg(42, "/start"), {42})
        methods = [m for m, _ in self.sent]
        self.assertIn("sendMessage", methods)
        body = self.sent[0][1]["data"]["text"]
        self.assertNotIn("not on its access list", body)

    def test_unauthorized_private_user_gets_only_a_denial(self):
        b.handle_update(self.tg, 42, self._msg(999, "/start"), {42})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("not on its access list", self.sent[0][1]["data"]["text"])

    def test_unauthorized_user_cannot_extract_ids(self):
        upd = self._msg(999)
        upd["message"]["entities"] = [
            {"type": "custom_emoji", "custom_emoji_id": "111111111"}]
        b.handle_update(self.tg, 42, upd, {42})
        sent_text = " ".join(kw["data"]["text"] for _, kw in self.sent)
        self.assertNotIn("111111111", sent_text)

    def test_unauthorized_group_message_is_silently_ignored(self):
        upd = self._msg(999, chat_type="supergroup")
        upd["message"]["entities"] = [
            {"type": "custom_emoji", "custom_emoji_id": "111111111"}]
        b.handle_update(self.tg, 42, upd, {42})
        self.assertEqual(self.sent, [], "must not reply into a group")


class TestOffsetPersistence(unittest.TestCase):
    """A restart must not replay updates that were already handled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = b.OFFSET_FILE
        b.OFFSET_FILE = Path(self.tmp.name) / "state_emoji_bot.json"

    def tearDown(self):
        b.OFFSET_FILE = self._orig
        self.tmp.cleanup()

    def test_missing_file_starts_at_zero(self):
        self.assertEqual(b._load_offset(), 0)

    def test_roundtrip(self):
        b._save_offset(4242)
        self.assertEqual(b._load_offset(), 4242)

    def test_corrupt_file_falls_back_to_zero(self):
        b.OFFSET_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(b._load_offset(), 0)


if __name__ == "__main__":
    unittest.main()
