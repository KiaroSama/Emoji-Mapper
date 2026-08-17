"""Argument parsing on the coin CLIs, where the default branch is destructive.

`fetch_paprika`, `fetch_cmc` and `fetch_logos` all have a switch that decides
between a dry run and one that spends API quota, uploads with the owner's
credentials and rewrites the canonical ticker map. Nothing behind it asks for
confirmation, so an argument the parser does not recognise has to be a refusal.

These drive `main` directly, so no test here can reach a network.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._cli_fixtures import _load_standalone  # noqa: E402


class _StopBeforeNetwork(Exception):
    """Cuts main() off at its first side effect, so no test can go online."""


class AnUnrecognisedArgumentCannotSelectTheLiveBranch(unittest.TestCase):
    """A typo must fail closed, not publish.

    The coin fetchers decided their one destructive switch with
    ``dry = "--dry" in sys.argv``. Membership testing has no notion of an
    argument it does not recognise: ``--dryy``, ``--dr``, a stray positional or
    any unknown flag all left ``dry`` False, and False is the branch that spends
    the API quota, uploads to Telegram with the owner's credentials and rewrites
    the canonical ticker map. There is no confirmation prompt behind it.

    These drive ``main`` directly with the argv in question, so nothing here can
    reach a network: argparse must refuse before ``load_env()`` on line one of
    the body runs.
    """

    def _mods(self):
        import coins.fetch_cmc as cmc
        import coins.fetch_paprika as paprika
        logos = _load_standalone(ROOT / "coins" / "fetch_logos.py", "argv_logos")
        return {"fetch_paprika": paprika, "fetch_cmc": cmc, "fetch_logos": logos}

    def test_a_misspelled_dry_flag_is_refused_by_both_providers(self):
        for name, mod in self._mods().items():
            if name == "fetch_logos":
                continue
            for typo in ("--dryy", "--dry-run", "--dr", "-dry", "dry"):
                with self.subTest(module=name, argv=typo):
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        with self.assertRaises(SystemExit) as caught:
                            mod.main([typo])
                    # 2 is argparse's usage code, and it is what the launcher
                    # and CI read as "you typed something wrong".
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("usage:", err.getvalue())

    def test_the_real_dry_flag_still_parses(self):
        """The guard must not have been bought by breaking the flag itself."""
        for name, mod in self._mods().items():
            if name == "fetch_logos":
                continue
            with self.subTest(module=name):
                ap_seen = {}

                def fake_load_env(_seen=ap_seen):
                    _seen["reached"] = True
                    raise _StopBeforeNetwork

                with mock.patch.object(mod, "load_env", fake_load_env), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(_StopBeforeNetwork):
                        mod.main(["--dry"])
                self.assertTrue(ap_seen.get("reached"),
                                "--dry must be accepted, not rejected as unknown")

    def test_fetch_logos_page_count_is_not_read_at_import(self):
        """`fetch_logos.py --help` used to raise ValueError from int(sys.argv[1]).

        The module owned the command line at import, so it could not be
        imported by anything that had its own arguments -- including the test
        runner and the entry-point import smoke test.
        """
        logos = self._mods()["fetch_logos"]
        self.assertEqual(logos.MAX_PAGES, 40, "the default must be a constant")

        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as caught:
                logos.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("usage:", out.getvalue())

        for bad in ("notanumber", "0", "-3"):
            with self.subTest(pages=bad):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        logos.main([bad])
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
