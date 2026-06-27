"""Tests for emojikit.logsetup redaction (the secret-safety guarantee).

These avoid calling setup_logging() (which configures the global root logger);
they exercise the pure redaction logic and the formatter, which is what keeps
secrets out of logs.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import logsetup as L  # noqa: E402

TOKEN = "***REMOVED-HISTORICAL***"


class TestRedaction(unittest.TestCase):
    def test_token_shaped_is_masked(self):
        out = L.redact(f"using {TOKEN} now")
        self.assertNotIn(TOKEN, out)
        self.assertIn("[REDACTED]", out)

    def test_bot_url_is_masked(self):
        out = L.redact(f"GET /bot{TOKEN}/getMe")
        self.assertNotIn(TOKEN, out)
        self.assertIn("/bot[REDACTED]/", out)

    def test_registered_secret_value_is_masked(self):
        secret = "***REMOVED-CMC_API_KEY***"  # CMC-key-shaped (32 hex)
        L.register_secret(secret)
        self.assertNotIn(secret, L.redact(f"key={secret}"))

    def test_short_value_not_registered(self):
        L.register_secret("abc")  # too short to register
        self.assertIn("abc", L.redact("abc"))

    def test_content_key_and_ids_preserved(self):
        # Content hashes and numeric custom_emoji_ids must NOT be redacted.
        s = "key s:01f6c1284261975d0000934b7fe6f3c3 cid 5899781975"
        self.assertEqual(L.redact(s), s)

    def test_formatter_redacts_record(self):
        L._RUN["id"] = "deadbeef"
        fmt = L._HumanFormatter("[%(levelname)s] [%(run_id)s] %(message)s")
        rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                                "token %s", (TOKEN,), None)
        out = fmt.format(rec)
        self.assertNotIn(TOKEN, out)
        self.assertIn("deadbeef", out)


if __name__ == "__main__":
    unittest.main()
