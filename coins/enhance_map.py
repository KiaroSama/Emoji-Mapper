"""Improve inventory coverage: map chain-suffixed tickers to the base logo.

Many inventory tickers are the same coin on another chain (e.g. 1inchbsc,
avaxc, bnbbsc, brettbase, chiparb). The base coin's logo exists, so strip known
network suffixes and reuse the base ticker's custom_emoji_id, then re-fill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"

# Network suffixes (longest first to strip greedily and safely).
SUFFIXES = ["mainnet", "erc20", "bep20", "trc20", "polygon", "base", "matic",
            "avax", "bsc", "arb", "ton", "sol", "trx", "eth", "op", "c"]


def main() -> int:
    ticker_to_id: dict[str, str] = json.loads((ROOT / "ticker_to_id.json").read_text("utf-8"))
    have = set(ticker_to_id)

    inv_text = INV.read_text(encoding="utf-8")
    inv_tickers = set(re.findall(r"ticker:\s*(\S+)", inv_text.lower()))

    added = 0
    for t in sorted(inv_tickers):
        if t in have:
            continue
        for suf in SUFFIXES:
            if t.endswith(suf) and len(t) > len(suf) + 1:
                base = t[: -len(suf)]
                if base in have:
                    ticker_to_id[t] = ticker_to_id[base]
                    added += 1
                    break

    (ROOT / "ticker_to_id.json").write_text(
        json.dumps(ticker_to_id, ensure_ascii=False, indent=1), encoding="utf-8")
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
