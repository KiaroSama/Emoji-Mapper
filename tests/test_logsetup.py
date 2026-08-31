"""Tests for emojikit.logsetup redaction (the secret-safety guarantee).

These avoid calling setup_logging() (which configures the global root logger);
they exercise the pure redaction logic and the formatter, which is what keeps
secrets out of logs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import logsetup as L  # noqa: E402

# Synthetic, never-issued credentials. Real secrets must never appear in a
# tracked file -- test_no_real_secret_is_committed() enforces that.
TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class TestRetentionIsReadWhenItIsUsed(unittest.TestCase):
    """A documented .env setting must not be decided at import time.

    load_env() runs inside main(), which is AFTER this module is imported, so a
    value set only in .env was read strictly too late and every run silently
    used the built-in default -- while .env.example advertised the setting as
    supported. build_pack.api_base() documents and solves the same hazard.
    """

    def setUp(self):
        self._had = os.environ.pop("EMOJI_LOG_RETENTION_DAYS", None)

    def tearDown(self):
        os.environ.pop("EMOJI_LOG_RETENTION_DAYS", None)
        if self._had is not None:
            os.environ["EMOJI_LOG_RETENTION_DAYS"] = self._had

    def test_a_value_set_after_import_is_honoured(self):
        self.assertEqual(L.log_retention_days(), L.LOG_RETENTION_DEFAULT_DAYS)
        os.environ["EMOJI_LOG_RETENTION_DAYS"] = "7"
        self.assertEqual(L.log_retention_days(), 7,
                         "the retention window was frozen at import time")

    def test_prune_takes_its_default_at_call_time(self):
        os.environ["EMOJI_LOG_RETENTION_DAYS"] = "0"
        # 0 disables pruning; if the default were bound at import this would
        # still be the built-in 30 and the call would go looking for old files.
        self.assertEqual(L.prune_old_logs(), 0)

    def test_a_malformed_value_falls_back_instead_of_raising(self):
        os.environ["EMOJI_LOG_RETENTION_DAYS"] = "not-a-number"
        self.assertEqual(L.log_retention_days(), L.LOG_RETENTION_DEFAULT_DAYS)
        # A bare int() here once raised at IMPORT, so logging -- and with it
        # every entry point that imports it -- died before argparse could speak.
        self.assertTrue(callable(L.setup_logging))

    def test_a_negative_value_cannot_mean_prune_everything(self):
        os.environ["EMOJI_LOG_RETENTION_DAYS"] = "-1"
        self.assertEqual(L.log_retention_days(), 0)


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


class TestRunOutcome(unittest.TestCase):
    """The summary line must state how the run actually ended."""

    def setUp(self):
        self._orig = L._RUN.get("exit_code")

    def tearDown(self):
        # _RUN is process-global and the atexit summary reads it; leaving a
        # test value behind makes the suite's own final log line lie.
        L._RUN["exit_code"] = self._orig

    def test_exit_code_is_recorded_and_returned(self):
        self.assertEqual(L.record_exit_code(3), 3)
        self.assertEqual(L._RUN["exit_code"], 3)


class TestLogRetention(unittest.TestCase):
    """Each run writes a new file, so old ones must age out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = L.LOG_DIR
        L.LOG_DIR = Path(self.tmp.name)

    def tearDown(self):
        L.LOG_DIR = self._orig
        self.tmp.cleanup()

    def _log(self, name, age_days):
        p = L.LOG_DIR / name
        p.write_text("x", encoding="utf-8")
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
        return p

    def test_old_logs_are_pruned_and_recent_ones_kept(self):
        old = self._log("old.log", 90)
        new = self._log("new.log", 1)
        self.assertEqual(L.prune_old_logs(keep_days=30), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_retention_can_be_disabled(self):
        old = self._log("old.log", 900)
        self.assertEqual(L.prune_old_logs(keep_days=0), 0)
        self.assertTrue(old.exists())


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
