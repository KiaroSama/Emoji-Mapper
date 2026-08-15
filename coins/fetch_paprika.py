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

import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from build_pack import (AmbiguousUploadError, LockBusy, Telegram, exclusive_lock,
                        ingest_exit_code, load_env, load_keywords, safe_int_env,
                        write_json_atomic)
from emojikit.media import _dhash, hamming
# The pipeline's single definition of "this image is effectively empty".
from make_emoji_pngs import _is_blank

ROOT = Path(__file__).resolve().parent
EMOJI = ROOT / "logos" / "emoji"
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"
STATE = ROOT / "rebuild_state.json"
TICKER_IDS = ROOT / "ticker_to_id.json"
CACHE = ROOT / "paprika_matches.json"  # resumable {ticker: {id, conf, name}}
KEYWORDS_CSV = ROOT / "keywords.csv"
# One lock for the whole coin pack family. fetch_paprika, fetch_cmc and
# verify_logos --fix all mutate the SAME live sets, so they must exclude each
# other by a fixed name -- not by whichever state file each happens to read.
PACK_LOCK = ROOT / "coin_pack.lock"
SET_BASE = "gvcryptoemoji"
SET_TITLE = "@GodVerify Crypto Emoji"
# A live sticker within this perceptual distance of the PNG we uploaded IS that
# upload: Telegram re-encodes PNG to WEBP, so identical content still differs by
# a bit or two.
SAME_IMAGE_MAX = 8

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
USER_ID = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)
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
    """Crop to alpha bbox, fit into a transparent 100x100 RGBA canvas.

    Returns False -- writing nothing -- for a blank provider image. A fully
    transparent placeholder used to pass straight through here (no alpha bbox
    means nothing was cropped) and be uploaded as an empty emoji. The source is
    judged by the same visible-alpha rule as the rest of the pipeline, BEFORE
    the fit: scaling a handful of stray pixels up to 100px would hide the very
    emptiness we are testing for.
    """
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return False
    if _is_blank(im):
        return False
    im = im.crop(im.split()[3].getbbox())  # _is_blank guarantees a bbox
    w, h = im.size
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
    write_json_atomic(CACHE, cache)


# --------------------------------------------------------------------------- #
# Publishing (shared by fetch_paprika and fetch_cmc)
# --------------------------------------------------------------------------- #
def live_stickers(tg: Telegram, name: str) -> list[dict]:
    """Live stickers of a set. Raises rather than guessing an empty set."""
    return tg.get_sticker_set(name).get("stickers", [])


def match_by_image(tg: Telegram, candidates: list[dict], png: Path) -> str:
    """custom_emoji_id of the candidate whose image is the one we uploaded.

    Only reached when a sticker we did not add appeared while we were adding:
    both are then equally "new", so identity has to come from the content.
    """
    want = _dhash(Image.open(png).convert("RGBA"))
    hits: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for st in candidates:
            dest = Path(tmp) / str(st.get("file_unique_id") or st["file_id"])
            try:
                tg.download_file(str(st["file_id"]), dest)
                got = _dhash(Image.open(dest).convert("RGBA"))
            except Exception as exc:  # noqa: BLE001 - an unreadable candidate
                print(f"  compare failed for {st.get('custom_emoji_id')}: {exc}",
                      flush=True)
                continue
            if hamming(want, got) <= SAME_IMAGE_MAX:
                hits.append(str(st["custom_emoji_id"]))
    if len(hits) != 1:
        raise RuntimeError(f"{len(candidates)} stickers appeared and {len(hits)} "
                           f"match {png.name}; refusing to guess an emoji id")
    return hits[0]


def added_emoji_id(tg: Telegram, name: str, before_ids: set[str], png: Path) -> str:
    """The custom_emoji_id of the sticker WE just put into ``name``.

    Never positional. Reading the ids off the tail (``cids[-len(added):]``)
    attributes an emoji id to the wrong ticker as soon as one add fails, a
    sticker is edited by hand, or another run appends in between.
    """
    new = [s for s in live_stickers(tg, name)
           if str(s.get("custom_emoji_id")) not in before_ids]
    if not new:
        raise RuntimeError(f"no new sticker is live in {name}")
    if len(new) == 1:
        return str(new[0]["custom_emoji_id"])
    return match_by_image(tg, new, png)


def _mutate(call, *args, **kw) -> None:
    """Run a set-growing Telegram call, tolerating an ambiguous outcome.

    An ambiguous failure must never be re-sent (that is what duplicates an
    emoji); the caller's identity check reads live state and decides.
    """
    try:
        call(*args, **kw)
    except AmbiguousUploadError as exc:
        print(f"  {exc}; deciding from live state", flush=True)


def publish_logos(tg: Telegram, tickers: list[str],
                  ticker_to_id: dict[str, str]) -> tuple[int, int]:
    """Add one emoji per ticker to the coin pack family; returns (added, failed).

    Both fetchers used to carry their own copy of this loop, and both had
    drifted into the same two defects: a blind retry of the non-idempotent add
    (a timeout after Telegram applied it duplicates the emoji) and reading the
    new emoji ids off the tail of the set.
    """
    keywords = load_keywords(KEYWORDS_CSV)
    added = failed = 0
    try:
        with exclusive_lock(PACK_LOCK):
            bot = tg.get_me()["username"]
            state = json.loads(STATE.read_text("utf-8"))
            last = sorted(state["sets"], key=lambda x: x["index"])[-1]
            set_index, set_name = last["index"], last["name"]
            for tk in tickers:
                png = EMOJI / f"{tk}.png"
                kw = keywords.get(tk, tk)
                try:
                    live = live_stickers(tg, set_name)
                    if len(live) >= PER_SET:
                        index = set_index + 1
                        name = f"{SET_BASE}{index}_by_{bot}"
                        title = f"{SET_TITLE} {index}"
                        _mutate(tg.create_set, USER_ID, name, title, png,
                                EMOJI_CHAR, kw)
                        cid = added_emoji_id(tg, name, set(), png)
                        # Record the set only once it is verifiably live: a
                        # phantom entry here sends every later add to a set that
                        # does not exist.
                        set_index, set_name = index, name
                        state["sets"].append({"index": index, "name": name,
                                              "title": title})
                        write_json_atomic(STATE, state)
                    else:
                        before_ids = {str(s.get("custom_emoji_id")) for s in live}
                        # expected_before makes a retry after a network failure
                        # verify the add instead of repeating it.
                        _mutate(tg.add_sticker, USER_ID, set_name, png, EMOJI_CHAR,
                                kw, expected_before=len(live))
                        cid = added_emoji_id(tg, set_name, before_ids, png)
                except Exception as exc:  # noqa: BLE001 - one coin must not stop the batch
                    # ponytail: an id we cannot verify is simply not recorded, so
                    # the next run re-adds that coin. Bounded by the lock and by
                    # these batches being a handful of coins; give the fetchers a
                    # build_pack-style in-flight ledger if they ever run unattended.
                    print(f"  add failed {tk}: {exc}", flush=True)
                    failed += 1
                    continue
                ticker_to_id[tk] = cid
                write_json_atomic(TICKER_IDS, ticker_to_id)
                added += 1
                time.sleep(0.3)
    except LockBusy as exc:
        print(f"ERROR: {exc}", flush=True)
        return 0, len(tickers)
    return added, failed


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
    failed = 0
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
            failed += 1
            continue
        if to_emoji_png(data, EMOJI / f"{tk}.png"):
            fetched.append(tk)
            print(f"  got logo: {tk} <- {cid}", flush=True)
        else:
            print(f"  unusable logo: {tk} ({cid})", flush=True)
            failed += 1

    print(f"fetched logos: {len(fetched)}", flush=True)
    if not fetched:
        print("nothing to add.", flush=True)
        return ingest_exit_code(0, failed)

    # Add to the last not-full set, overflow to new sets.
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    added, add_failed = publish_logos(tg, fetched, ticker_to_id)
    filled, total = refill_inventory(ticker_to_id)
    print(f"added {added} stickers; inventory filled: {filled}/{total}", flush=True)
    return ingest_exit_code(added, failed + add_failed)


if __name__ == "__main__":
    raise SystemExit(main())
