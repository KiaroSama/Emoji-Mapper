"""The documented lock order and non-reentrancy, enforced mechanically.

Two rules govern every mutating tool in this repository:

1. **Order.** The pack-family lock is taken FIRST, `canonical_map_lock()` second.
   A tool that only rewrites the canonical map takes the map lock alone. Any path
   that takes them the other way round can deadlock against a provider holding
   them in the documented order.
2. **Non-reentrancy.** `build_pack.exclusive_lock` is a file lock with an
   ownership token; it is NOT reentrant. Acquiring a lock a function already
   holds -- directly or through a helper it calls -- cannot succeed.

Both rules are invisible to anyone reading a single file: the violation lives in
the pairing of a caller in one module with a helper in another. Reviews miss that
kind of defect reliably, and the last two audits both found one. So this checks
it by walking the AST of every tool instead of trusting a reader.

Deliberately AST-only: no imports, no execution, no network. It sees code that
never runs in the suite just as well as code that does.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAP_LOCK = "canonical_map_lock"
PACK_LOCK = "exclusive_lock"

# Scan executable modules and coin tools after the package relocation.
SOURCES = sorted(
    [p for p in (ROOT / "emojikit").glob("*.py")
     if '\nif __name__ == "__main__":' in p.read_text(encoding="utf-8")]
    + list((ROOT / "coins").glob("*.py"))
)


def _acquired_in(node: ast.AST) -> set[str]:
    """Lock kinds acquired by `with` statements anywhere inside ``node``."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, (ast.With, ast.AsyncWith)):
            continue
        for item in sub.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name in (MAP_LOCK, PACK_LOCK):
                    found.add(MAP_LOCK if name == MAP_LOCK else PACK_LOCK)
    return found


def _ordered_pairs(fn: ast.AST) -> list[tuple[str, str]]:
    """(outer, inner) lock pairs from directly nested `with` statements."""
    pairs: list[tuple[str, str]] = []

    def kinds_of(w: ast.With | ast.AsyncWith) -> list[str]:
        out = []
        for item in w.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name == MAP_LOCK:
                    out.append(MAP_LOCK)
                elif name == PACK_LOCK:
                    out.append(PACK_LOCK)
        return out

    def walk(node: ast.AST, held: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.With, ast.AsyncWith)):
                mine = kinds_of(child)
                # `with a(), b():` is nesting too -- a is outer, b is inner.
                for outer in held:
                    for inner in mine:
                        pairs.append((outer, inner))
                for i, outer in enumerate(mine):
                    for inner in mine[i + 1:]:
                        pairs.append((outer, inner))
                walk(child, held + mine)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue          # a nested def runs on its own, not here
            else:
                walk(child, held)

    walk(fn, [])
    return pairs


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _called_names(node: ast.AST) -> set[str]:
    """Bare names of every function called inside ``node``.

    Attribute calls (`tg.foo()`) are ignored: locks are taken by module-level
    helpers, and resolving methods would need type inference for no extra reach.
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None)
            if name:
                out.add(name)
    return out


class TheDocumentedLockOrderHolds(unittest.TestCase):
    def setUp(self):
        self.trees = {}
        for path in SOURCES:
            try:
                self.trees[path] = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:      # pragma: no cover - a broken tree
                self.fail(f"{path.name} does not parse: {exc}")

    def test_the_map_lock_is_never_taken_before_a_pack_lock(self):
        """Order inversion is the deadlock: two tools, opposite orders."""
        bad = []
        for path, tree in self.trees.items():
            for fn in _functions(tree):
                for outer, inner in _ordered_pairs(fn):
                    if outer == MAP_LOCK and inner == PACK_LOCK:
                        bad.append(f"{path.name}:{fn.lineno} {fn.name}()")
        self.assertEqual(bad, [], "these take canonical_map_lock() OUTSIDE a "
                                  "pack lock, inverting the documented order: "
                                  + ", ".join(bad))

    def test_no_function_nests_a_lock_it_already_holds(self):
        """exclusive_lock is not reentrant; nesting the same kind cannot pass."""
        bad = []
        for path, tree in self.trees.items():
            for fn in _functions(tree):
                for outer, inner in _ordered_pairs(fn):
                    if outer == inner:
                        bad.append(f"{path.name}:{fn.lineno} {fn.name}() "
                                   f"nests {inner}")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_a_lock_holder_never_calls_a_helper_that_takes_the_same_lock(self):
        """The cross-function form of the same trap.

        `verify_logos.repoint()` opens its own `canonical_map_lock()`. A caller
        that decides to hold the map lock across a wider span -- exactly what
        binding a replacement to one map requires -- deadlocks on repoint's
        acquisition. Nothing in either function reads as wrong on its own.
        """
        # Which module-level functions acquire which lock, per file.
        takes: dict[Path, dict[str, set[str]]] = {}
        for path, tree in self.trees.items():
            takes[path] = {fn.name: _acquired_in(fn) for fn in _functions(tree)}

        bad = []
        for path, tree in self.trees.items():
            local = takes[path]
            for fn in _functions(tree):
                held = _acquired_in(fn)
                if not held:
                    continue
                # Only calls made INSIDE the with-block matter, but a function
                # that takes a lock at all is nearly always structured with the
                # body inside it; treat the whole function conservatively and
                # let an explicit exemption handle a real false positive.
                for callee in _called_names(fn) - {fn.name}:
                    clash = held & local.get(callee, set())
                    if clash:
                        bad.append(f"{path.name}:{fn.lineno} {fn.name}() holds "
                                   f"{'/'.join(sorted(clash))} and calls "
                                   f"{callee}(), which takes it again")
        self.assertEqual(bad, [], "; ".join(bad))


class TheCheckerActuallySeesAViolation(unittest.TestCase):
    """A checker that cannot fail proves nothing. Feed it the known traps."""

    def _pairs(self, src: str):
        fn = next(_functions(ast.parse(src)))
        return _ordered_pairs(fn)

    def test_it_flags_an_inverted_order(self):
        self.assertIn(
            (MAP_LOCK, PACK_LOCK),
            self._pairs("def f():\n"
                        "    with canonical_map_lock():\n"
                        "        with exclusive_lock(P):\n"
                        "            pass\n"))

    def test_it_flags_a_reentrant_nest(self):
        self.assertIn(
            (MAP_LOCK, MAP_LOCK),
            self._pairs("def f():\n"
                        "    with canonical_map_lock():\n"
                        "        with canonical_map_lock():\n"
                        "            pass\n"))

    def test_it_reads_one_with_statement_as_nesting(self):
        self.assertIn(
            (PACK_LOCK, MAP_LOCK),
            self._pairs("def f():\n"
                        "    with exclusive_lock(P), canonical_map_lock():\n"
                        "        pass\n"))

    def test_the_documented_order_is_not_flagged(self):
        pairs = self._pairs("def f():\n"
                            "    with exclusive_lock(P):\n"
                            "        with canonical_map_lock():\n"
                            "            pass\n")
        self.assertEqual(pairs, [(PACK_LOCK, MAP_LOCK)])

    def test_it_scans_a_real_and_non_empty_set_of_tools(self):
        """Guards against a glob that quietly matches nothing."""
        names = {p.name for p in SOURCES}
        for expected in ("build_pack.py", "fetch_paprika.py", "verify_logos.py",
                         "rebuild_dedup.py", "remap_ids.py"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
