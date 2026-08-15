"""Tests for emojikit.logsetup redaction (the secret-safety guarantee).

These avoid calling setup_logging() (which configures the global root logger);
they exercise the pure redaction logic and the formatter, which is what keeps
secrets out of logs.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import logsetup as L  # noqa: E402

# Synthetic, never-issued credentials. Real secrets must never appear in a
# tracked file -- test_no_real_secret_is_committed() enforces that.
TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


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
        secret = "00000000000000000000000000000000"  # CMC-key-shaped (32 hex)
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


class TestNoCommittedSecrets(unittest.TestCase):
    """Guard: no real secret value may appear in a git-tracked file.

    Regression for the real bot token + CMC key that were committed as test
    fixtures in this very module. Skips where there is nothing to check
    (no local .env, or git unavailable -- e.g. a CI checkout without secrets).
    """

    MAX_BYTES = 2 * 1024 * 1024  # skip anything larger; no secret hides there

    def test_no_real_secret_is_committed(self):
        env = ROOT / ".env"
        if not env.exists():
            self.skipTest("no local .env to check against")

        secrets_by_key = {}
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Only genuine secrets: logsetup.SECRET_ENV_KEYS is the single
            # definition of which env values must never be exposed. Public
            # config (bot usernames, owner id) legitimately appears in docs.
            if len(value) >= 12 and any(s in key for s in L.SECRET_ENV_KEYS):
                secrets_by_key[key] = value

        if not secrets_by_key:
            self.skipTest(".env holds no secret-length values")

        try:
            out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, timeout=60,
                                 capture_output=True, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git unavailable")

        leaks = []
        for name in out.decode("utf-8", "replace").split("\0"):
            if not name:
                continue
            path = ROOT / name
            try:
                if not path.is_file() or path.stat().st_size > self.MAX_BYTES:
                    continue
                text = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            for key, value in secrets_by_key.items():
                if value in text:
                    leaks.append(f"{key} -> {name}")

        # Never print the value itself, only which key leaked into which file.
        self.assertEqual(leaks, [], f"secret value(s) found in tracked files: {leaks}")


if __name__ == "__main__":
    unittest.main()
