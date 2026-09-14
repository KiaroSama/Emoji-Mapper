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

# Sampled WHILE THIS MODULE IS IMPORTED, which is the only moment that answers
# "was the guard there before anything could read .env?". Reading it later is
# useless: test_outbound_connections_are_refused below does `import tests`, so
# the package installs itself MID-RUN and every later look reports "installed"
# -- long after the credentials are already in os.environ.
_GUARD_PRESENT_AT_IMPORT = "tests" in sys.modules

TIMEOUT = 120          # a clean import is well under a second


def _entry_points() -> list[str]:
    """Importable module names for every first-party executable script."""
    mods = [f"emojikit.{p.stem}" for p in sorted((ROOT / "emojikit").glob("*.py"))
            if '\nif __name__ == "__main__":' in p.read_text(encoding="utf-8")]
    mods += [f"coins.{p.stem}" for p in sorted((ROOT / "coins").glob("*.py"))
             if p.stem != "__init__"]
    return mods


class EntryPointsImport(unittest.TestCase):
    def test_discovery_found_the_expected_tools(self):
        mods = _entry_points()
        # Sanity-check the discovery itself, so an empty glob cannot make this
        # suite vacuously green.
        self.assertGreaterEqual(len(mods), 15, f"only found {mods}")
        self.assertEqual(list(ROOT.glob("*.py")), [], "Python modules belong in emojikit")
        for expected in ("emojikit.build_pack", "emojikit.emoji_bot", "emojikit.panel",
                         "coins.verify_logos", "coins.rebuild_dedup"):
            self.assertIn(expected, mods)

    def test_command_modules_preserve_project_data_paths(self):
        import importlib

        for name in _entry_points():
            if name.startswith("emojikit."):
                module = importlib.import_module(name)
                if hasattr(module, "ROOT"):
                    self.assertEqual(module.ROOT, ROOT, name)
        from emojikit import emoji_bot
        from emojikit import logsetup
        self.assertEqual(emoji_bot.OFFSET_FILE, ROOT / "state_emoji_bot.json")
        self.assertEqual(logsetup.LOG_DIR, ROOT / "logs")

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

    def test_the_guard_was_installed_before_the_test_modules(self):
        """Canary: the guard must be there FIRST, not merely eventually.

        The old form asked whether socket.connect was patched AT ASSERT TIME.
        Under `unittest discover -s tests` (no -t) the tests package is skipped
        at import, so the modules load as `test_x` and nothing is scrubbed --
        but test_outbound_connections_are_refused below does `import tests`, the
        patch appears mid-run, and this canary passed while the real .env values
        had ALREADY been read into os.environ at import time. A check a later
        import can satisfy cannot fail at the moment it is needed.
        """
        self.assertTrue(
            _GUARD_PRESENT_AT_IMPORT,
            "the hermetic guard was NOT active when the test modules were "
            "imported: this suite was started in a way that skips "
            "tests/__init__.py. Use: "
            "python -m unittest discover -s tests -t . -p \"test_*.py\"")

    # NOTE: these deliberately do NOT assert that credential-shaped names are
    # absent from os.environ. Other tests legitimately inject FAKE tokens via
    # mock.patch.dict, and asserting on the name made this suite fail depending
    # on which module ran first. What matters is that the REAL values in .env
    # cannot reach a test, which is what these check.

    def test_the_real_dotenv_values_are_not_loaded(self):
        import os
        from pathlib import Path
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if not env_file.exists():
            self.skipTest("no local .env to leak")
        leaked = []
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if len(value) >= 12 and os.environ.get(key.strip()) == value:
                leaked.append(key.strip())
        self.assertEqual(leaked, [], f"real .env values visible to tests: {leaked}")

    def test_load_env_is_a_no_op_under_the_guard(self):
        """A module calling load_env() at import must not undo the scrub."""
        import os

        from emojikit import build_pack
        before = dict(os.environ)
        build_pack.load_env()
        self.assertEqual(dict(os.environ), before,
                         "load_env() modified the environment despite "
                         "EMOJI_MAPPER_NO_DOTENV")

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
