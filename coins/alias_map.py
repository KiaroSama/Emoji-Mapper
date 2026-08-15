"""Fill more inventory tickers by matching coin NAME to an existing logo.

For each still-blank inventory ticker, normalize its coin name (drop chain info
in parentheses and generic words) and look up a logo with the same normalized
name. Safe: it only matches when the coin NAMES agree AND exactly one logo
answers to that normalized name; anything ambiguous is reported and left
unmapped rather than guessed. Then it re-fills the inventory.
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import csv
import json
import re
from pathlib import Path

from build_pack import EXIT_FAILED, LockBusy, canonical_map_lock, write_json_atomic

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
    text = INV.read_text(encoding="utf-8")
    blocks = re.findall(r"##\s*(\S+)\s*[\u2014-]+\s*(.+?)\n\s*ticker:\s*(\S+)", text)
    map_path = ROOT / "ticker_to_id.json"
    try:
        # ONE lock across the COMPLETE read-modify-write. Atomic replacement
        # stops a truncated file; it does not stop a LOST UPDATE, where another
        # writer's ids are silently dropped because this run rewrites the whole
        # file from a snapshot taken before that writer finished. Reading inside
        # the lock is the half that makes the lock worth taking.
        with canonical_map_lock():
            ticker_to_id: dict[str, str] = json.loads(
                map_path.read_text("utf-8"))
            have = set(ticker_to_id)

            # Build normalized-name -> {logo tickers} (only logos we have an id
            # for). A set, not the first hit: normalization deliberately drops
            # words like "token"/"network", so distinct coins collapse onto the
            # same key and keeping whichever the CSV listed first silently
            # invented a wrong alias.
            name_to_logos: dict[str, set[str]] = {}
            with open(ROOT / "keywords.csv", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    t = row["ticker"].lower()
                    if t in have:
                        key = norm(row.get("name") or "")
                        if key:
                            name_to_logos.setdefault(key, set()).add(t)

            added = ambiguous = 0
            for _hdr, name, tk in blocks:
                tk = tk.lower()
                if tk in have:
                    continue
                key = norm(name)
                logos = name_to_logos.get(key) or set()
                # Only auto-apply when the match is unambiguous. Several logos
                # pointing at the SAME emoji id is still one answer; different
                # ids is a guess.
                ids = {ticker_to_id[c] for c in logos}
                if len(ids) == 1:
                    logo = sorted(logos)[0]
                    ticker_to_id[tk] = ticker_to_id[logo]
                    added += 1
                    print(f"  alias {tk} -> {logo} ({name.strip()})", flush=True)
                elif ids:
                    ambiguous += 1
                    print(f"  AMBIGUOUS {tk} ({name.strip()}): '{key}' matches "
                          f"{', '.join(sorted(logos))} -> left unmapped, "
                          f"resolve by hand", flush=True)

            # write_text truncates first, so a crash mid-write left the whole
            # mapping empty or half-parsed; write_json_atomic renames a complete
            # file into place instead.
            write_json_atomic(map_path, ticker_to_id)
    except LockBusy as exc:
        print(f"ERROR: {exc}", flush=True)
        return EXIT_FAILED
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
