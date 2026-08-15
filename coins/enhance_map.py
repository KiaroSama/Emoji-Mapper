"""Improve inventory coverage: map chain-suffixed tickers to the base logo.

Many inventory tickers are the same coin on another chain (e.g. 1inchbsc,
avaxc, bnbbsc, brettbase, chiparb). The base coin's logo exists, so strip known
network suffixes and reuse the base ticker's custom_emoji_id, then re-fill.
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine from the root.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import json
import re
from pathlib import Path

from build_pack import write_json_atomic

ROOT = Path(__file__).resolve().parent
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"

# Network suffixes (longest first to strip greedily and safely).
#
# A single-character suffix is NOT identity evidence: stripping "c" made "ghc"
# (Galaxy Heroes Coin) inherit the logo of "gh" (Greyhound), and "zbc" (Zebec)
# that of "zb" (ZeroByte). The only genuine one-character cases are named
# below instead of guessed.
SUFFIXES = ["mainnet", "erc20", "bep20", "trc20", "polygon", "base", "matic",
            "avax", "bsc", "arb", "ton", "sol", "trx", "eth", "op"]

# Verified same-asset aliases that no suffix rule can derive safely.
EXPLICIT_ALIASES = {
    "avaxc": "avax",   # Avalanche C-Chain
    "bttc": "btt",     # BitTorrent Chain
}


def main() -> int:
    ticker_to_id: dict[str, str] = json.loads((ROOT / "ticker_to_id.json").read_text("utf-8"))
    have = set(ticker_to_id)

    inv_text = INV.read_text(encoding="utf-8")
    inv_tickers = set(re.findall(r"ticker:\s*(\S+)", inv_text.lower()))

    added = 0
    for t in sorted(inv_tickers):
        if t in have:
            continue
        base = EXPLICIT_ALIASES.get(t)
        if base is None:
            for suf in SUFFIXES:
                if t.endswith(suf) and len(t) > len(suf) + 1:
                    base = t[: -len(suf)]
                    break
        if base and base in have:
            ticker_to_id[t] = ticker_to_id[base]
            added += 1

    write_json_atomic(ROOT / "ticker_to_id.json", ticker_to_id)
    print(f"added {added} chain-variant mappings", flush=True)

    # Re-fill inventory.
    lines = inv_text.split("\n")
    t_re = re.compile(r"^\s*ticker:\s*(?P<v>.+?)\s*$")
    p_re = re.compile(r"^(?P<prefix>\s*)premium-id:\s*.*$")
    cur = None
    filled = total = 0
    for i, ln in enumerate(lines):
        m = t_re.match(ln)
        if m:
            cur = m.group("v").strip().lower()
            total += 1
            continue
        pm = p_re.match(ln)
        if pm and cur is not None:
            eid = ticker_to_id.get(cur, "")
            lines[i] = (f"{pm.group('prefix')}premium-id: {eid}").rstrip()
            if eid:
                filled += 1
            cur = None
    OUT_INV.write_text("\n".join(lines), encoding="utf-8")
    print(f"inventory filled: {filled}/{total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
