"""The sandbox must isolate the ACCOUNT, not only the catalog.

`scripts/panel_sandbox.py` exists because an agent's synthetic drags rewrote
the owner's real ordering. It cloned the catalog and then handed the child its
own environment untouched, so the cloned-catalog panel still called
`_detect_bot_username()` -> `load_env()` -> `getMe` against live Telegram with
the real token. Cloning the data while keeping the credentials is half a
sandbox.

These assert the scrub directly rather than launching a panel: the value under
test is a dict, and a subprocess would prove the same thing far more slowly.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import panel_sandbox  # noqa: E402 - needs the paths above


class TheSandboxChildGetsNoCredentials(unittest.TestCase):

    def child(self, **extra):
        with mock.patch.dict(os.environ, extra, clear=False):
            return panel_sandbox.scrubbed_environment()

    def test_dotenv_reading_is_switched_off_for_the_child(self):
        """The flag load_env() already honours for the suite; without it the
        child re-reads the real .env no matter what the parent environment holds."""
        self.assertEqual(self.child()["EMOJI_MAPPER_NO_DOTENV"], "1")

    def test_an_exported_token_does_not_survive_into_the_child(self):
        child = self.child(GENERAL_BOT_TOKEN="111:live", COIN_BOT_TOKEN="222:live")
        self.assertNotIn("GENERAL_BOT_TOKEN", child)
        self.assertNotIn("COIN_BOT_TOKEN", child)

    def test_every_key_the_template_names_is_dropped(self):
        """`.env.example` is the authoritative list, so a credential added to the
        project later is scrubbed without editing this file."""
        template = ROOT / ".env.example"
        if not template.is_file():
            self.skipTest("no .env.example in this checkout")
        named = [line.split("=", 1)[0].strip()
                 for line in template.read_text(encoding="utf-8").splitlines()
                 if "=" in line and not line.lstrip().startswith("#")]
        self.assertTrue(named, ".env.example names no keys; the scrub proves nothing")
        child = self.child(**{key: "live-value" for key in named})
        self.assertEqual([key for key in named if key in child], [])

    def test_a_credential_shape_is_dropped_even_when_the_template_is_silent(self):
        child = self.child(SOME_PRIVATE_TOKEN="x", DEPLOY_SECRET="y", OWNER_ID="7")
        for key in ("SOME_PRIVATE_TOKEN", "DEPLOY_SECRET", "OWNER_ID"):
            self.assertNotIn(key, child)

    def test_the_ordinary_environment_still_reaches_the_child(self):
        """Scrubbing must not break the child: it still has to find Python."""
        child = self.child(EMOJI_SANDBOX_CANARY="kept")
        self.assertEqual(child.get("EMOJI_SANDBOX_CANARY"), "kept")
        self.assertIn("PATH", {key.upper() for key in child})


if __name__ == "__main__":
    unittest.main()
