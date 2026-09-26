"""The operator's identities come only from configuration (docs/adr/0001).

Unset is unknown: every accessor a publish depends on must STOP and name the
key, never fall back to a value -- a fallback is someone else's identity.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emojikit import operator_config as oc
from emojikit.errors import OperatorConfigMissing

KEYS = ("BRAND_LOGO_BOTS", "BRAND_LOGO_PATH", "BRAND_LOGO_KEYWORDS",
        "COIN_PACK_BASE", "COIN_PACK_TITLE")


class OperatorConfigTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in KEYS:
            os.environ.pop(k, None)

    def test_an_unset_key_is_named_in_the_stop(self):
        os.environ["COIN_PACK_TITLE"] = "t"
        with self.assertRaises(OperatorConfigMissing) as cm:
            oc.require("COIN_PACK_BASE", "COIN_PACK_TITLE")
        self.assertIn("COIN_PACK_BASE", str(cm.exception))
        self.assertNotIn("COIN_PACK_TITLE", str(cm.exception))

    def test_unset_logo_bots_stops_but_empty_means_none(self):
        with self.assertRaises(OperatorConfigMissing) as cm:
            oc.brand_logo(False, None)
        self.assertIn("BRAND_LOGO_BOTS", str(cm.exception))
        self.assertEqual(oc.brand_logo_bots(strict=False), frozenset())
        os.environ["BRAND_LOGO_BOTS"] = ""
        self.assertEqual(oc.brand_logo(False, None), (frozenset(), None))

    def test_bot_list_ignores_case_and_at(self):
        os.environ["BRAND_LOGO_BOTS"] = "@YourEmojiBot, other_bot"
        self.assertEqual(oc.brand_logo_bots(), {"youremojibot", "other_bot"})

    def test_a_listed_bot_needs_an_existing_logo(self):
        os.environ["BRAND_LOGO_BOTS"] = "YourEmojiBot"
        with self.assertRaises(OperatorConfigMissing) as cm:
            oc.brand_logo(False, None)
        self.assertIn("BRAND_LOGO_PATH", str(cm.exception))
        os.environ["BRAND_LOGO_PATH"] = "private/does-not-exist.png"
        with self.assertRaises(OperatorConfigMissing):
            oc.brand_logo(False, None)

    def test_a_configured_logo_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            logo = Path(tmp) / "logo.png"
            logo.write_bytes(b"png")
            os.environ["BRAND_LOGO_BOTS"] = "YourEmojiBot"
            os.environ["BRAND_LOGO_PATH"] = str(logo)
            self.assertEqual(oc.brand_logo(False, None), ({"youremojibot"}, str(logo)))

    def test_a_relative_logo_path_is_under_the_project_root(self):
        os.environ["BRAND_LOGO_PATH"] = "private/brand-logo.png"
        self.assertEqual(oc.brand_logo_path(), oc.ROOT / "private" / "brand-logo.png")

    def test_disabled_needs_no_configuration(self):
        self.assertEqual(oc.brand_logo(True, None), (frozenset(), None))

    def test_keywords_default_to_logo(self):
        self.assertEqual(oc.brand_logo_keywords(), ["logo"])
        os.environ["BRAND_LOGO_KEYWORDS"] = "yourbrand, logo"
        self.assertEqual(oc.brand_logo_keywords(), ["yourbrand", "logo"])


if __name__ == "__main__":
    unittest.main()
