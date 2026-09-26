"""A suite that exists on disk but runs nowhere is worse than no suite.

The Python matrix runs full discovery, so an ordinary new module is covered the
moment it is written. The browser suites are the exception: they opt out of the
matrix (`NUMERA_EMOJI_MAPPER_NO_BROWSER_TESTS=1`, because they test JavaScript and
would download Chromium once per Python version) and run in their own job from
a HAND-MAINTAINED list of module names.

A hand-maintained list is the thing that silently goes stale, so this is the
mechanical guard over it. Adding `tests/test_panel_queues.py` without adding it
to that line would have left fourteen tests passing locally and running nowhere
in CI.

Plain text, not YAML: pulling a parser in for one `run:` line would add a
dependency to the suite, and the assertion is only "does this module name
appear in that step".
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# What marks a suite as needing the browser job: it IMPORTS the harness.
BROWSER_FIXTURE = "_panel_browser_fixtures"

# What marks a suite as needing the native-Windows job: it SAYS SO. There is no
# import to key on -- the nine suites share no module that the rest of the tree
# does not also import -- because "Linux cannot exercise this" is a judgement
# about the behaviour under test, not a property of the code. So the judgement
# is written down in the suite it belongs to and enforced from here, both ways:
# a marked suite missing from the job, and a job entry that no longer marks
# itself, are each a silent hole.
WINDOWS_MARKER = "RUNS_ON_NATIVE_WINDOWS"


def _imports_harness(path: Path) -> bool:
    """An import, not a mention.

    A substring search matched this very file, which names the fixture in a
    constant and imports nothing -- and it would match a docstring or a comment
    just as happily. The import graph is the real question, so ask the AST.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and BROWSER_FIXTURE in node.module:
                return True
            if node.module == "tests" and any(
                    a.name == BROWSER_FIXTURE for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(BROWSER_FIXTURE in a.name for a in node.names):
                return True
    return False


def _browser_suites() -> list[str]:
    return [f"tests.{p.stem}" for p in sorted(TESTS.glob("test_*.py"))
            if _imports_harness(p)]


def _declares_windows(path: Path) -> bool:
    """A module-level assignment, not a substring.

    The same lesson the browser scan learned: a substring matches this file,
    which names the marker in a constant and claims nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == WINDOWS_MARKER
                for t in node.targets):
            return isinstance(node.value, ast.Constant) and node.value.value is True
    return False


def _windows_suites() -> list[str]:
    return [f"tests.{p.stem}" for p in sorted(TESTS.glob("test_*.py"))
            if _declares_windows(p)]


def _windows_step(workflow: str) -> str:
    """Only the windows-safety job's own run block, so a name that appears in
    another job cannot make this guard pass."""
    head = workflow.find("windows-safety:")
    return workflow[head:] if head >= 0 else ""


class EveryBrowserSuiteIsClaimedByCi(unittest.TestCase):
    def setUp(self):
        if not WORKFLOW.is_file():
            self.skipTest(f"no workflow at {WORKFLOW}")
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_scan_finds_the_browser_suites(self):
        """A guard that matches nothing passes forever and proves nothing."""
        self.assertGreaterEqual(len(_browser_suites()), 2,
                                "the browser-suite scan stopped finding them")

    def test_each_one_is_named_in_the_browser_job(self):
        missing = [m for m in _browser_suites() if m not in self.workflow]
        self.assertEqual(missing, [],
                         f"browser suite(s) run nowhere in CI: {missing}. Add "
                         f"them to the `Panel browser tests` step in ci.yml.")

    def test_the_matrix_opts_out_explicitly(self):
        """The opt-out must stay written down, not be left to chance.

        The module raises rather than skipping when playwright is missing, so
        without this variable the whole Python matrix would fail on a runner
        that has no browser -- and with it set silently, a green matrix would
        mean less than it looks.
        """
        self.assertIn("NUMERA_EMOJI_MAPPER_NO_BROWSER_TESTS", self.workflow)

    def test_no_browser_suite_is_run_twice(self):
        """Once in its own job is the whole point of the opt-out."""
        for module in _browser_suites():
            self.assertEqual(self.workflow.count(module), 1,
                             f"{module} is named more than once in CI")


class EveryNativeWindowsSuiteIsClaimedByCi(unittest.TestCase):
    """The Windows job runs a hand-written list, the same shape of hole the
    browser job had. Nothing fails when a new native-safety suite is added and
    forgotten: it runs green on Linux and never executes on Windows."""

    def setUp(self):
        if not WORKFLOW.is_file():
            self.skipTest(f"no workflow at {WORKFLOW}")
        self.step = _windows_step(WORKFLOW.read_text(encoding="utf-8"))

    def test_the_job_exists(self):
        self.assertTrue(self.step, "the windows-safety job is gone from ci.yml")

    def test_the_scan_finds_the_marked_suites(self):
        """A guard that matches nothing passes forever and proves nothing."""
        self.assertGreaterEqual(len(_windows_suites()), 5,
                                "the native-Windows marker scan stopped finding them")

    def test_each_marked_suite_is_named_in_the_job(self):
        missing = [m for m in _windows_suites() if m not in self.step]
        self.assertEqual(missing, [],
                         f"native-Windows suite(s) run nowhere on Windows: {missing}. "
                         f"Add them to the windows-safety step in ci.yml.")

    def test_every_name_in_the_job_still_claims_windows(self):
        """The other direction: a renamed or repurposed suite leaves a name in
        the job that quietly matches nothing."""
        named = [w for w in self.step.split()
                 if w.startswith("tests.test_")]
        self.assertTrue(named, "the windows-safety step names no suites")
        stale = [m for m in named if m not in _windows_suites()]
        self.assertEqual(stale, [],
                         f"windows-safety names suite(s) that do not declare "
                         f"{WINDOWS_MARKER}: {stale}")


if __name__ == "__main__":
    unittest.main()
