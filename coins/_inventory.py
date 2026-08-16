"""Shared inventory parsing / re-filling for the coin tools.

Four tools (alias_map, enhance_map, fetch_paprika, rebuild_dedup) each carried a
verbatim copy of the premium-id re-fill loop, and the ticker->base resolver had
already drifted: enhance_map knew that ``avaxc`` is Avalanche and ``bttc`` is
BitTorrent Chain while fetch_paprika's ``base_ticker`` did not, so "which base
does this ticker belong to" had two answers depending on which tool ran. One
definition each, here.

Paths are arguments rather than module constants so every caller keeps its own
INV/OUT_INV -- which is also what lets the tests point a tool at a temp file.
"""

from __future__ import annotations

import re
from pathlib import Path

# Network suffixes (longest first to strip greedily and safely).
#
# A single-character suffix is NOT identity evidence: stripping "c" made "ghc"
# (Galaxy Heroes Coin) inherit the logo of "gh" (Greyhound), and "zbc" (Zebec)
# that of "zb" (ZeroByte). The only genuine one-character cases are named in
# EXPLICIT_ALIASES instead of guessed.
SUFFIXES = ["mainnet", "erc20", "bep20", "trc20", "polygon", "base", "matic",
            "avax", "bsc", "arb", "ton", "sol", "trx", "eth", "op"]

# Verified same-asset aliases that no suffix rule can derive safely.
EXPLICIT_ALIASES = {
    "avaxc": "avax",   # Avalanche C-Chain
    "bttc": "btt",     # BitTorrent Chain
}

_BLOCK_RE = re.compile(r"##\s*(\S+)\s*[—-]+\s*(.+?)\n\s*ticker:\s*(\S+)")
_TICKER_RE = re.compile(r"^\s*ticker:\s*(?P<v>.+?)\s*$")
_PREMIUM_RE = re.compile(r"^(?P<prefix>\s*)premium-id:\s*.*$")


def norm(s: str) -> str:
    """Lowercase, drop parenthetical qualifiers, keep only [a-z0-9]."""
    s = re.sub(r"\(.*?\)", " ", s.lower())
    return "".join(re.findall(r"[a-z0-9]+", s))


def base_ticker(t: str) -> str:
    """The asset a chain-variant ticker belongs to (e.g. kibabsc -> kiba)."""
    alias = EXPLICIT_ALIASES.get(t)
    if alias:
        return alias
    for suf in SUFFIXES:
        if t.endswith(suf) and len(t) > len(suf) + 1:
            return t[: -len(suf)]
    return t


def parse_missing(have: set[str], inv: Path) -> list[tuple[str, str]]:
    """(coin name, ticker) for every inventory entry not already in ``have``."""
    text = inv.read_text(encoding="utf-8")
    return [(name.strip(), tk.lower()) for _h, name, tk in _BLOCK_RE.findall(text)
            if tk.lower() not in have]


def refill_inventory(ticker_to_id: dict[str, str], inv: Path,
                     out_inv: Path) -> tuple[int, int]:
    """Rewrite ``inv`` into ``out_inv`` with every premium-id resolved.

    Returns (filled, total). A ticker with no id gets a blank line rather than a
    stale one, so an entry that lost its sticker is visibly unresolved.
    """
    lines = inv.read_text(encoding="utf-8").split("\n")
    cur = None
    filled = total = 0
    for i, ln in enumerate(lines):
        m = _TICKER_RE.match(ln)
        if m:
            cur = m.group("v").strip().lower()
            total += 1
            continue
        pm = _PREMIUM_RE.match(ln)
        if pm and cur is not None:
            eid = ticker_to_id.get(cur, "")
            lines[i] = (f"{pm.group('prefix')}premium-id: {eid}").rstrip()
            if eid:
                filled += 1
            cur = None
    out_inv.write_text("\n".join(lines), encoding="utf-8")
    return filled, total
