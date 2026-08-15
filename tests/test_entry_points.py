"""Every executable entry point must import cleanly, and stay hermetic.

`coins/verify_logos.py` once imported a name build_pack no longer exported, so
the script died with ImportError before main() ever ran -- and nothing noticed,
because no test imported it. This discovers the entry points instead of listing
them, so a new tool is covered the moment it is added.

It is a TEST rather than a CI step on purpose: GitHub Actions for this
repository is currently blocked before its first step by an account billing
condition, so a CI-only guard would protect nothing today.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TIMEOUT = 120          # a clean import is well under a second


def _entry_points() -> list[str]:
    """Importable module names for every first-party executable script."""
    mods = [p.stem for p in sorted(ROOT.glob("*.py"))
            if not p.stem.startswith("test_")]
    mods += [f"coins.{p.stem}" for p in sorted((ROOT / "coins").glob("*.py"))
             if p.stem != "__init__"]
    return mods


class EntryPointsImport(unittest.TestCase):
    def test_discovery_found_the_expected_tools(self):
        mods = _entry_points()
        # Sanity-check the discovery itself, so an empty glob cannot make this
        # suite vacuously green.
        self.assertGreaterEqual(len(mods), 15, f"only found {mods}")
        for expected in ("build_pack", "emoji_bot", "panel",
                         "coins.verify_logos", "coins.rebuild_dedup"):
            self.assertIn(expected, mods)

    def test_every_entry_point_imports(self):
        """Import each in its own interpreter, so import-time side effects
        (logging handlers, env loading) cannot leak between modules or into
        the rest of the suite."""
        failures = []
        for mod in _entry_points():
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, r'%s'); "
                 "import tests; import importlib; importlib.import_module(%r)"
                 % (ROOT, mod)],
                capture_output=True, text=True, timeout=TIMEOUT, cwd=ROOT)
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()
                failures.append(f"{mod}: {tail[-1] if tail else 'unknown error'}")
        self.assertEqual(failures, [], "entry points failed to import:\n"
                                       + "\n".join(failures))


class SuiteIsHermetic(unittest.TestCase):
    """The guard in tests/__init__.py must actually hold."""

    def test_credentials_are_not_visible(self):
        import os
        for name in ("TELEGRAM_BOT_TOKEN", "GENERAL_BOT_TOKEN", "CMC_API_KEY"):
            self.assertNotIn(name, os.environ,
                             f"{name} is visible to tests; a stray real call "
                             f"could authenticate")

    def test_dotenv_cannot_put_them_back(self):
        """A module calling load_env() at import must not undo the scrub."""
        import os

        import build_pack
        build_pack.load_env()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)

    def test_outbound_connections_are_refused(self):
        import socket

        import tests
        # Own the socket so the refused attempt cannot leak an unclosed fd.
        sock = socket.socket()
        try:
            with self.assertRaises(tests.NetworkAccessDenied):
                sock.connect(("api.telegram.org", 443))
        finally:
            sock.close()

    def test_loopback_still_works(self):
        """The panel tests bind a real local server; that must keep working."""
        import socket
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            with socket.create_connection(srv.getsockname(), timeout=5):
                pass
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
