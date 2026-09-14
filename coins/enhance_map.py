"""Improve inventory coverage: map chain-suffixed tickers to the base logo.

Many inventory tickers are the same coin on another chain (e.g. 1inchbsc,
avaxc, bnbbsc, brettbase, chiparb). The base coin's logo exists, so strip known
network suffixes and reuse the base ticker's custom_emoji_id, then re-fill.
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine from the root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import json
import re
from pathlib import Path

from build_pack import (EXIT_FAILED)
from emojikit.packstate import (LockBusy, canonical_map_lock, write_json_atomic)
# The suffix list and the explicit aliases used to live here while the fetchers
# carried their own copy without the aliases, so "which asset is avaxc" had two
# answers. One resolver now, shared.
from coins._inventory import base_ticker, refill_inventory

ROOT = Path(__file__).resolve().parent
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"


def main() -> int:
    inv_text = INV.read_text(encoding="utf-8")
    inv_tickers = set(re.findall(r"ticker:\s*(\S+)", inv_text.lower()))
    map_path = ROOT / "ticker_to_id.json"
    try:
        # ONE lock across the COMPLETE read-modify-write, and the read happens
        # INSIDE it. This rewrites the whole canonical map, so a snapshot taken
        # before waiting for the lock would silently discard every id the writer
        # we queued behind had just added.
        with canonical_map_lock():
            ticker_to_id: dict[str, str] = json.loads(
                map_path.read_text("utf-8"))
            have = set(ticker_to_id)

            added = 0
            for t in sorted(inv_tickers):
                if t in have:
                    continue
                # base_ticker returns t unchanged when no rule applies, and t is
                # known not to be in `have`, so that case maps nothing.
                base = base_ticker(t)
                if base in have:
                    ticker_to_id[t] = ticker_to_id[base]
                    added += 1

            write_json_atomic(map_path, ticker_to_id)
    except LockBusy as exc:
        print(f"ERROR: {exc}", flush=True)
        return EXIT_FAILED
    print(f"added {added} chain-variant mappings", flush=True)

    filled, total = refill_inventory(ticker_to_id, INV, OUT_INV)
    print(f"inventory filled: {filled}/{total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
