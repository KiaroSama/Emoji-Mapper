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

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from build_pack import Telegram, load_env
# Reuse proven helpers from the CoinPaprika fetcher.
from fetch_paprika import (
    EMOJI, STATE, TICKER_IDS, EMOJI_CHAR, PER_SET, USER_ID,
    base_ticker, classify, http_bytes, load_keywords, parse_missing,
    refill_inventory, to_emoji_png,
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
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None  # symbol not found / bad request -> no match
            print(f"  http retry {a}: HTTP {exc.code}", flush=True)
            time.sleep(min(4 * a, 20))
        except Exception as exc:  # noqa: BLE001
            print(f"  http retry {a}: {exc}", flush=True)
            time.sleep(min(4 * a, 20))
    return None


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
    for tk, cid in resolved:
        url = get_logo_url(headers, cid)
        if not url:
            print(f"  no logo: {tk} (cmc:{cid})", flush=True)
            continue
        data = http_bytes(url)
        if not data:  # 128x128 may not exist; fall back to default 64x64
            data = http_bytes(url.replace("/128x128/", "/64x64/"))
        if not data:
            print(f"  download failed: {tk}", flush=True)
            continue
        if to_emoji_png(data, EMOJI / f"{tk}.png"):
            fetched.append(tk)
            print(f"  got logo: {tk} <- cmc:{cid}", flush=True)

    print(f"fetched logos: {len(fetched)}", flush=True)
    if not fetched:
        print("nothing to add.", flush=True)
        return 0

    # Add to the last not-full set, overflow to new sets (mirrors fetch_paprika.py).
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    bot = tg.get_me()["username"]
    state = json.loads(STATE.read_text("utf-8"))
    sets = sorted(state["sets"], key=lambda x: x["index"])
    last = sets[-1]
    set_index = last["index"]
    set_name = last["name"]
    in_set = len(tg._call("getStickerSet", data={"name": set_name}).get("stickers", []))
    keywords = load_keywords()

    added_order: list[tuple[str, str]] = []
    for tk in fetched:
        png = EMOJI / f"{tk}.png"
        kw = keywords.get(tk, tk)
        try:
            if in_set >= PER_SET:
                set_index += 1
                set_name = f"gvcryptoemoji{set_index}_by_{bot}"
                tg.create_set(USER_ID, set_name, f"@GodVerify Crypto Emoji {set_index}",
                              png, EMOJI_CHAR, kw)
                state["sets"].append({"index": set_index, "name": set_name,
                                      "title": f"@GodVerify Crypto Emoji {set_index}"})
                STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
                in_set = 1
            else:
                tg.add_sticker(USER_ID, set_name, png, EMOJI_CHAR, kw)
                in_set += 1
            added_order.append((set_name, tk))
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            print(f"  add failed {tk}: {exc}", flush=True)

    per_set_added: dict[str, list[str]] = defaultdict(list)
    for sn, tk in added_order:
        per_set_added[sn].append(tk)
    for sn, tks in per_set_added.items():
        cids = [str(s.get("custom_emoji_id", ""))
                for s in tg._call("getStickerSet", data={"name": sn}).get("stickers", [])]
        tail = cids[-len(tks):]
        for tk, cid in zip(tks, tail):
            ticker_to_id[tk] = cid

    TICKER_IDS.write_text(json.dumps(ticker_to_id, ensure_ascii=False, indent=1), "utf-8")
    filled, total = refill_inventory(ticker_to_id)
    print(f"added {len(added_order)} stickers; inventory filled: {filled}/{total}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
