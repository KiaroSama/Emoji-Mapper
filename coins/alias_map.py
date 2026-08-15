"""Fill more inventory tickers by matching coin NAME to an existing logo.

For each still-blank inventory ticker, normalize its coin name (drop chain info
in parentheses and generic words) and look up a logo with the same normalized
name. Safe: it only matches when the coin NAMES agree AND exactly one logo
answers to that normalized name; anything ambiguous is reported and left
unmapped rather than guessed. Then it re-fills the inventory.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"

DROP = {"network", "token", "protocol", "finance", "labs", "the", "bridged",
        "new", "coin", "inc", "io", "governance", "chain", "erc20", "bep20",
        "trc20", "usd"}


def norm(name: str) -> str:
    name = re.sub(r"\(.*?\)", " ", name.lower())   # drop "(BSC)" etc.
    words = re.findall(r"[a-z0-9]+", name)
    words = [w for w in words if w not in DROP]
    return "".join(words)


def main() -> int:
    ticker_to_id: dict[str, str] = json.loads((ROOT / "ticker_to_id.json").read_text("utf-8"))
    have = set(ticker_to_id)

    # Build normalized-name -> {logo tickers} (only logos we have an id for).
    # A set, not the first hit: normalization deliberately drops words like
    # "token"/"network", so distinct coins collapse onto the same key and
    # keeping whichever the CSV listed first silently invented a wrong alias.
    name_to_logos: dict[str, set[str]] = {}
    kp = ROOT / "keywords.csv"
    with open(kp, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t = row["ticker"].lower()
            if t in have:
                key = norm(row.get("name") or "")
                if key:
                    name_to_logos.setdefault(key, set()).add(t)

    text = INV.read_text(encoding="utf-8")
    blocks = re.findall(r"##\s*(\S+)\s*[\u2014-]+\s*(.+?)\n\s*ticker:\s*(\S+)", text)

    added = ambiguous = 0
    for _hdr, name, tk in blocks:
        tk = tk.lower()
        if tk in have:
            continue
        key = norm(name)
        logos = name_to_logos.get(key) or set()
        # Only auto-apply when the match is unambiguous. Several logos pointing
        # at the SAME emoji id is still one answer; different ids is a guess.
        ids = {ticker_to_id[c] for c in logos}
        if len(ids) == 1:
            logo = sorted(logos)[0]
            ticker_to_id[tk] = ticker_to_id[logo]
            added += 1
            print(f"  alias {tk} -> {logo} ({name.strip()})", flush=True)
        elif ids:
            ambiguous += 1
            print(f"  AMBIGUOUS {tk} ({name.strip()}): '{key}' matches "
                  f"{', '.join(sorted(logos))} -> left unmapped, resolve by hand",
                  flush=True)

    (ROOT / "ticker_to_id.json").write_text(
        json.dumps(ticker_to_id, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"added {added} name-alias mappings "
          f"({ambiguous} left unmapped as ambiguous)", flush=True)

    # Re-fill inventory.
    lines = text.split("\n")
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
