"""Build keywords.csv from the logos already on disk.

Uses CoinGecko's lightweight /coins/list endpoint (a single request, no
pagination) to map each ticker to a full coin name for the keywords column,
then lists every SVG and PNG logo present in logos/.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SVG_DIR = ROOT / "logos" / "svg"
PNG_DIR = ROOT / "logos" / "png"
OUT = ROOT / "keywords.csv"
LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
MARKETS_PAGES = 12   # market-cap order: gives canonical names for the top coins
HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher; local tool)"}


def _get_json(url: str, *, retries: int = 6):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = min(8.0 * attempt, 40.0)
            print(f"  retry {attempt}/{retries}: {exc} (wait {wait:.0f}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(str(last))


def fetch_canonical_names() -> dict[str, str]:
    """ticker(lower) -> canonical coin name.

    Prefers market-cap order (the most prominent coin for each ticker), then
    fills the long tail from the full /coins/list.
    """
    names: dict[str, str] = {}
    # 1) Market-cap order first (highest cap wins per ticker -> canonical name).
    for page in range(1, MARKETS_PAGES + 1):
        url = (f"{MARKETS_URL}?vs_currency=usd&order=market_cap_desc&per_page=250"
               f"&page={page}&sparkline=false")
        try:
            data = _get_json(url)
        except RuntimeError as exc:
            print(f"  markets page {page} unavailable: {exc}", flush=True)
            break
        if not isinstance(data, list) or not data:
            break
        for c in data:
            sym = str(c.get("symbol", "")).lower().strip()
            name = str(c.get("name", "")).strip()
            if sym and sym not in names:
                names[sym] = name
        print(f"  markets page {page}: {len(names)} canonical names so far.", flush=True)
        time.sleep(12.0)
    # 2) Long tail from /coins/list (only fills tickers not already set).
    try:
        for c in _get_json(LIST_URL):
            sym = str(c.get("symbol", "")).lower().strip()
            name = str(c.get("name", "")).strip()
            if sym and sym not in names:
                names[sym] = name
    except RuntimeError as exc:
        print(f"  coins/list unavailable: {exc}", flush=True)
    print(f"name map: {len(names)} entries.", flush=True)
    return names


def main() -> int:
    names = fetch_canonical_names()
    rows: dict[str, dict] = {}
    for p in sorted(SVG_DIR.glob("*.svg")):
        t = p.stem.lower()
        rows[t] = {"ticker": t, "name": names.get(t, ""), "format": "svg",
                   "file": f"logos/svg/{t}.svg"}
    for p in sorted(PNG_DIR.glob("*.png")):
        t = p.stem.lower()
        rows.setdefault(t, {"ticker": t, "name": names.get(t, ""), "format": "png",
                            "file": f"logos/png/{t}.png"})

    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "name", "format", "file", "keywords"])
        for r in sorted(rows.values(), key=lambda x: x["ticker"]):
            kw = r["ticker"] if not r["name"] else f"{r['ticker']}, {r['name']}"
            w.writerow([r["ticker"], r["name"], r["format"], r["file"], kw])

    svg = sum(1 for r in rows.values() if r["format"] == "svg")
    png = sum(1 for r in rows.values() if r["format"] == "png")
    named = sum(1 for r in rows.values() if r["name"])
    print(f"keywords.csv written: {len(rows)} logos ({svg} svg, {png} png), {named} with names.",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
