"""Fetch logos for the last few inventory coins via the CoinMarketCap Pro API.

These coins are unavailable (no logo) on CoinGecko and CoinPaprika. CMC's free
Basic plan covers them. For each still-missing inventory ticker:
  1) /v1/cryptocurrency/map  -> candidate ids by symbol (base + full ticker)
  2) pick a confident match (name-exact or symbol) -- conservative, like the
     CoinPaprika flow (a wrong logo is worse than a blank entry)
  3) /v2/cryptocurrency/info -> logo url; download the 128x128 variant
  4) convert to a 100x100 emoji PNG, add to the last not-full pack, capture the
     new custom_emoji_id, and re-fill the inventory.

The CMC API key is read from .env as CMC_API_KEY (never printed).

Usage:
  python fetch_cmc.py --dry   # search + report candidates only
  python fetch_cmc.py         # download confident matches, add to packs
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import json
import os
import sys
import time
import urllib.parse

from build_pack import Telegram, ingest_exit_code, load_env
from coins import _http
from coins._inventory import base_ticker
# Reuse proven helpers from the CoinPaprika fetcher -- including the ONE
# verified publisher, so this fetcher cannot drift back into its own copy.
# Package-qualified so the module also imports as ``coins.fetch_cmc``.
from coins.fetch_paprika import (
    TICKER_IDS, classify, http_bytes, incoming_dir, parse_missing,
    publish_logos, refill_inventory, to_emoji_png,
)

MAP = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/map?symbol="
INFO = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/info?id="
SLEEP = 1.0


def cmc_headers() -> dict:
    key = os.environ.get("CMC_API_KEY", "").strip()
    if not key:
        raise SystemExit("CMC_API_KEY missing in .env")
    return {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}


def cmc_json(url: str, headers: dict, retries: int = 4):
    r = _http.get(url, headers=headers, retries=retries, stop_on=(400, 404))
    if r is None or r.status_code in (400, 404):
        return None  # symbol not found / bad request -> no match
    return r.json()


def map_candidates(headers: dict, name: str, ticker: str):
    """Return CMC candidate list [{id,name,symbol}] for base+full ticker."""
    seen: dict[int, dict] = {}
    for sym in {ticker.upper(), base_ticker(ticker).upper()}:
        d = cmc_json(MAP + urllib.parse.quote(sym), headers)
        time.sleep(SLEEP)
        for c in (d or {}).get("data", []) or []:
            seen[c["id"]] = {"id": str(c["id"]), "name": c.get("name", ""),
                             "symbol": c.get("symbol", "")}
    return list(seen.values())


def get_logo_url(headers: dict, cmc_id: str) -> str | None:
    d = cmc_json(INFO + cmc_id, headers)
    time.sleep(SLEEP)
    info = (d or {}).get("data", {}).get(cmc_id) or {}
    logo = info.get("logo")
    if not logo:
        return None
    # Prefer a larger variant; CMC serves 64x64 by default.
    return logo.replace("/64x64/", "/128x128/")


def main() -> int:
    dry = "--dry" in sys.argv
    load_env()
    headers = cmc_headers()
    ticker_to_id: dict[str, str] = json.loads(TICKER_IDS.read_text("utf-8"))
    have = set(ticker_to_id)
    missing = parse_missing(have)
    print(f"missing to resolve via CMC: {len(missing)} | dry={dry}", flush=True)

    resolved: list[tuple[str, str]] = []  # (ticker, cmc_id)
    for name, tk in missing:
        cands = map_candidates(headers, name, tk)
        cid, conf = classify(cands, name, tk)
        if cid and conf in ("name-exact", "symbol"):
            resolved.append((tk, cid))
            print(f"  MATCH {tk:12s} <- cmc:{cid}  [{conf}]  ({name})", flush=True)
        else:
            print(f"  none  {tk:12s}  {conf}  ({name})", flush=True)

    print(f"\nconfident={len(resolved)}", flush=True)
    if dry or not resolved:
        if dry:
            print("dry run: no downloads/pack changes.", flush=True)
        return 0

    # Resolve logos + build emoji PNGs.
    fetched: list[str] = []
    failed = 0
    for tk, cid in resolved:
        url = get_logo_url(headers, cid)
        if not url:
            print(f"  no logo: {tk} (cmc:{cid})", flush=True)
            failed += 1
            continue
        data = http_bytes(url)
        if not data:  # 128x128 may not exist; fall back to default 64x64
            data = http_bytes(url.replace("/128x128/", "/64x64/"))
        if not data:
            print(f"  download failed: {tk}", flush=True)
            failed += 1
            continue
        if to_emoji_png(data, incoming_dir() / f"{tk}.png"):
            fetched.append(tk)
            print(f"  got logo: {tk} <- cmc:{cid}", flush=True)
        else:
            print(f"  unusable logo: {tk} (cmc:{cid})", flush=True)
            failed += 1

    print(f"fetched logos: {len(fetched)}", flush=True)
    if not fetched:
        print("nothing to add.", flush=True)
        return ingest_exit_code(0, failed)

    # Same verified publisher as fetch_paprika: locked, duplicate-proof adds and
    # emoji ids read by identity.
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    added, add_failed = publish_logos(tg, fetched, ticker_to_id)
    filled, total = refill_inventory(ticker_to_id)
    print(f"added {added} stickers; inventory filled: {filled}/{total}", flush=True)
    return ingest_exit_code(added, failed + add_failed)


if __name__ == "__main__":
    raise SystemExit(main())
