"""C01: a test double must accept what the real client accepts.

The successful Python 3.11 CI log carried this line over and over:

    FakeTelegram.send_message() got an unexpected keyword argument 'disable_preview'

`announce_packs` passes `disable_preview`, two fakes did not take it, and the
call sites swallow a failed notification on purpose -- a channel post must not
sink a finished publish. So every happy-path announcement in those suites was
silently exercising the ERROR path, and the tests were green because nothing
asserted the message had actually been sent.

Fixing the two fakes is not the fix; the next one drifts the same way. The real
guard is mechanical: compare each fake's signature against the client it stands
in for. `_bc_fixtures.FakeTG` being correct was never evidence about the others.

AST, not import: pulling every test module in would execute them, and the point
is a cheap structural check that runs even when a suite is broken.
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit.telegram_api import Telegram  # noqa: E402

TESTS = Path(__file__).resolve().parent


def _real_methods() -> dict[str, inspect.Signature]:
    """The public surface a fake could plausibly be standing in for."""
    out = {}
    for name, fn in vars(Telegram).items():
        if name.startswith("_") or not callable(fn):
            continue
        out[name] = inspect.signature(fn)
    return out


def _fakes() -> list[tuple[Path, str, str, ast.arguments]]:
    """(file, class, method, args) for every method in tests/ that shadows one."""
    real = _real_methods()
    found = []
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name in real:
                    found.append((path, cls.name, fn.name, fn.args))
    return found


class EveryFakeAcceptsTheRealCall(unittest.TestCase):
    def test_the_scan_finds_something(self):
        """A guard that matches nothing passes forever and proves nothing."""
        self.assertGreaterEqual(len(_fakes()), 5,
                                "the fake scan stopped finding test doubles")

    def test_no_fake_rejects_a_keyword_the_real_client_accepts(self):
        """The exact shape of the defect: a keyword-only argument left out.

        A fake may simplify -- it does not have to implement the behaviour --
        but it must not REFUSE a call the production code makes, because that
        turns a happy path into an exception the caller was written to swallow.
        """
        real = _real_methods()
        problems = []
        for path, cls, name, args in _fakes():
            if args.kwarg is not None:
                continue      # **anything accepts anything, whatever it is named
            taken = {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
            for param in real[name].parameters.values():
                if param.kind is param.KEYWORD_ONLY and param.name not in taken:
                    problems.append(
                        f"{path.name}:{cls}.{name} does not accept "
                        f"{param.name!r}, which Telegram.{name} defines")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_no_fake_takes_fewer_positionals_than_the_real_client(self):
        """Only the positionals a caller MUST supply.

        A defaulted one is not the fake's problem: `download_file(file_id,
        dest, retries=5)` is called with two arguments everywhere, and
        demanding the third would fail nine correct fakes to catch nothing.
        """
        real = _real_methods()
        problems = []
        for path, cls, name, args in _fakes():
            if args.vararg is not None:
                continue
            positional = len(args.posonlyargs) + len(args.args)
            wanted = sum(1 for p in real[name].parameters.values()
                         if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                         and p.default is p.empty)
            if positional < wanted:
                problems.append(
                    f"{path.name}:{cls}.{name} takes {positional} positional "
                    f"argument(s); Telegram.{name} passes {wanted}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
