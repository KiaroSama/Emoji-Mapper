"""Fetch logos for still-missing inventory coins from CoinPaprika.

CoinGecko search failed for ~29 obscure inventory coins. CoinPaprika
(free, no API key) covers many of them. This script searches CoinPaprika by
coin name (and ticker as fallback), picks a confident match, downloads the logo
from CoinPaprika's static CDN, converts it to a 100x100 emoji PNG, optionally
adds it to the last not-full Telegram pack, captures the new custom_emoji_id,
and re-fills the inventory.

Key design points:
- The logo URL is deterministic: https://static.coinpaprika.com/coin/{id}/logo.png
  so once the coin id is known from search, the image is fetched from the static
  CDN (not the metered API) -> only /search calls count against the free quota.
- The free /search quota is small and returns HTTP 402 when exhausted. On 402 the
  script stops cleanly and persists progress to paprika_matches.json, so a later
  run (after the quota window resets) resumes without re-searching resolved coins.
- Matching is conservative: only "name-exact" or "symbol" matches are accepted.
  Weaker matches are reported but NOT used (a wrong logo is worse than a blank).

Usage:
  python fetch_paprika.py --dry     # search + cache matches, no downloads/packs
  python fetch_paprika.py           # download cached/confident matches, add to packs
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from PIL import Image

from build_pack import Telegram, load_env

ROOT = Path(__file__).resolve().parent
EMOJI = ROOT / "logos" / "emoji"
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"
STATE = ROOT / "rebuild_state.json"
TICKER_IDS = ROOT / "ticker_to_id.json"
CACHE = ROOT / "paprika_matches.json"  # resumable {ticker: {id, conf, name}}

SEARCH = "https://api.coinpaprika.com/v1/search?c=currencies&limit=10&q="
COIN = "https://api.coinpaprika.com/v1/coins/"
LOGO_CDN = "https://static.coinpaprika.com/coin/{id}/logo.png"
HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher; local)"}
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
# Load .env at import so the owner id is available even when this module is
# imported by fetch_cmc.py (which reuses USER_ID).
load_env()
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
USER_ID = int(os.environ.get("PACK_OWNER_USER_ID", "0"))
SIZE = 100
SLEEP = 2.5  # seconds between metered /search calls

QUOTA_EXHAUSTED = object()  # sentinel returned by http_json on HTTP 402


def norm(s: str) -> str:
    """Lowercase, drop parenthetical qualifiers, keep only [a-z0-9]."""
    s = re.sub(r"\(.*?\)", " ", s.lower())
    return "".join(re.findall(r"[a-z0-9]+", s))


def base_ticker(t: str) -> str:
    """Strip common chain suffixes (e.g. kibabsc -> kiba)."""
    for suf in ("mainnet", "erc20", "bep20", "trc20", "polygon", "base", "matic",
                "avax", "bsc", "arb", "ton", "sol", "trx", "eth", "op"):
        if t.endswith(suf) and len(t) > len(suf) + 1:
            return t[: -len(suf)]
    return t


def http_json(url: str, retries: int = 4):
    """GET JSON. Returns dict, None (transient failure), or QUOTA_EXHAUSTED on 402."""
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 402:
                return QUOTA_EXHAUSTED  # free quota gone; do not retry
            print(f"  http retry {a}: HTTP {exc.code}", flush=True)
            time.sleep(min(4 * a, 20))
        except Exception as exc:  # noqa: BLE001
            print(f"  http retry {a}: {exc}", flush=True)
            time.sleep(min(4 * a, 20))
    return None


def http_bytes(url: str, retries: int = 3):
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(2 * a)
    return None


def to_emoji_png(data: bytes, dest: Path) -> bool:
    """Crop to alpha bbox, fit into a transparent 100x100 RGBA canvas."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return False
    bb = im.split()[3].getbbox()
    if bb:
        im = im.crop(bb)
    w, h = im.size
    if not w or not h:
        return False
    sc = min(SIZE / w, SIZE / h)
    nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(im, ((SIZE - nw) // 2, (SIZE - nh) // 2), im)
    canvas.save(dest, format="PNG", optimize=True)
    return True


def classify(cands: list[dict], name: str, ticker: str):
    """Pick best candidate. Returns (id, confidence) or (None, reason)."""
    if not cands:
        return None, "no-results"
    nn = norm(name)
    bt = base_ticker(ticker)
    for c in cands:  # 1) exact normalized name
        if norm(c.get("name", "")) == nn:
            return c["id"], "name-exact"
    for c in cands:  # 2) symbol equals full or base ticker
        if str(c.get("symbol", "")).lower() in (ticker, bt):
            return c["id"], "symbol"
    for c in cands:  # 3) partial (reported, not auto-used)
        cn = norm(c.get("name", ""))
        if cn and (cn in nn or nn in cn):
            return None, f"partial:{c['id']}"
    return None, f"no-confident-match(top={cands[0]['id']})"


def search_match(name: str, ticker: str):
    """Up to two metered /search calls. Returns (id, conf) | (None, reason) | QUOTA."""
    q1 = re.sub(r"\(.*?\)", "", name).strip()
    res = http_json(SEARCH + urllib.parse.quote(q1))
    if res is QUOTA_EXHAUSTED:
        return QUOTA_EXHAUSTED
    time.sleep(SLEEP)
    cands = (res or {}).get("currencies", [])
    cid, conf = classify(cands, name, ticker)
    if cid:
        return cid, conf
    # Fallback: search by ticker only when the name search was unhelpful.
    if not cands or conf.startswith("no-confident") or conf == "no-results":
        res2 = http_json(SEARCH + urllib.parse.quote(ticker))
        if res2 is QUOTA_EXHAUSTED:
            return QUOTA_EXHAUSTED
        time.sleep(SLEEP)
        cands2 = (res2 or {}).get("currencies", [])
        cid2, conf2 = classify(cands2, name, ticker)
        if cid2:
            return cid2, conf2
        return None, conf2 if cands2 else conf
    return None, conf


def load_keywords() -> dict[str, str]:
    kw: dict[str, str] = {}
    kp = ROOT / "keywords.csv"
    if kp.is_file():
        for row in csv.DictReader(open(kp, encoding="utf-8")):
            kw[row["ticker"].lower()] = row.get("keywords") or row["ticker"]
    return kw


def parse_missing(have: set[str]) -> list[tuple[str, str]]:
    text = INV.read_text(encoding="utf-8")
    blocks = re.findall(r"##\s*(\S+)\s*[\u2014-]+\s*(.+?)\n\s*ticker:\s*(\S+)", text)
    return [(name.strip(), tk.lower()) for _h, name, tk in blocks
            if tk.lower() not in have]


def refill_inventory(ticker_to_id: dict[str, str]) -> tuple[int, int]:
    text = INV.read_text(encoding="utf-8")
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
    return filled, total


def load_cache() -> dict[str, dict]:
    if CACHE.is_file():
        return json.loads(CACHE.read_text("utf-8"))
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")


def resolve_phase(missing, cache) -> bool:
    """Search CoinPaprika for unresolved coins. Returns True if quota was hit."""
    quota_hit = False
    for name, tk in missing:
        if tk in cache:
            continue
        r = search_match(name, tk)
        if r is QUOTA_EXHAUSTED:
            print("  HTTP 402: CoinPaprika free quota exhausted; stopping search.",
                  flush=True)
            quota_hit = True
            break
        cid, conf = r
        if cid and conf in ("name-exact", "symbol"):
            cache[tk] = {"id": cid, "conf": conf, "name": name}
            print(f"  MATCH {tk:12s} <- {cid}  [{conf}]  ({name})", flush=True)
        else:
            cache[tk] = {"id": None, "conf": conf, "name": name}
            tag = "partial" if conf.startswith("partial") else "none"
            print(f"  {tag:7s} {tk:12s}  {conf}  ({name})", flush=True)
        save_cache(cache)  # persist after every coin (resumable)
    return quota_hit


def main() -> int:
    dry = "--dry" in sys.argv
    load_env()
    ticker_to_id: dict[str, str] = json.loads(TICKER_IDS.read_text("utf-8"))
    have = set(ticker_to_id)
    missing = parse_missing(have)
    cache = load_cache()
    print(f"missing to resolve: {len(missing)} | cached: {len(cache)} | dry={dry}",
          flush=True)

    quota_hit = resolve_phase(missing, cache)

    confident = [(tk, v["id"]) for tk, v in cache.items()
                 if v.get("id") and tk not in have]
    print(f"\nconfident(new)={len(confident)} cached_total={len(cache)} "
          f"quota_hit={quota_hit}", flush=True)

    if dry:
        print("dry run: matches cached, no downloads/pack changes.", flush=True)
        return 0
    if not confident:
        print("no new confident matches to add.", flush=True)
        return 0

    # Download confident matches from the static CDN (not metered) -> emoji PNGs.
    # If the deterministic CDN path is missing (404), fall back to the /coins/{id}
    # API 'logo' field (metered, but quota is available when this path is reached).
    fetched: list[str] = []
    for tk, cid in confident:
        data = http_bytes(LOGO_CDN.format(id=cid))
        if not data:
            detail = http_json(COIN + cid)
            time.sleep(SLEEP)
            url = (detail or {}).get("logo") if detail is not QUOTA_EXHAUSTED else None
            if url and url != LOGO_CDN.format(id=cid):
                data = http_bytes(url)
        if not data:
            print(f"  download failed: {tk} ({cid})", flush=True)
            continue
        if to_emoji_png(data, EMOJI / f"{tk}.png"):
            fetched.append(tk)
            print(f"  got logo: {tk} <- {cid}", flush=True)

    print(f"fetched logos: {len(fetched)}", flush=True)
    if not fetched:
        print("nothing to add.", flush=True)
        return 0

    # Add to the last not-full set, overflow to new sets (mirrors fetch_missing.py).
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
                set_name = f"cryptoemoji{set_index}_by_{bot}"
                tg.create_set(USER_ID, set_name, f"@YourBrand Crypto Emoji {set_index}",
                              png, EMOJI_CHAR, kw)
                state["sets"].append({"index": set_index, "name": set_name,
                                      "title": f"@YourBrand Crypto Emoji {set_index}"})
                STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
                in_set = 1
            else:
                tg.add_sticker(USER_ID, set_name, png, EMOJI_CHAR, kw)
                in_set += 1
            added_order.append((set_name, tk))
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            print(f"  add failed {tk}: {exc}", flush=True)

    # Capture new custom_emoji_ids: appended stickers are at the tail, in add order.
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
