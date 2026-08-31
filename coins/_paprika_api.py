"""Talking to CoinPaprika: HTTP, candidate search, and logo decoding.

Split from coins/fetch_paprika.py, which publishes. This half touches
none of the state paths the tests redirect -- only the provider's own
constants -- which is why it could move without qualifying them.
"""

from __future__ import annotations

# This module lives in coins/; allow importing the shared engine from the
# project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import io
import logging
import re
import time
import urllib.parse
from pathlib import Path

from PIL import Image

from coins import _http
from coins._inventory import base_ticker, norm
from make_emoji_pngs import _is_blank

log = logging.getLogger("fetch_paprika")

SEARCH = "https://api.coinpaprika.com/v1/search?c=currencies&limit=10&q="
HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher; local)"}
SIZE = 100
SLEEP = 2.5  # seconds between metered /search calls
QUOTA_EXHAUSTED = object()  # sentinel returned by http_json on HTTP 402


def http_json(url: str, retries: int = 4):
    """GET JSON. Returns dict, None (transient failure), or QUOTA_EXHAUSTED on 402."""
    r = _http.get(url, headers=HEADERS, retries=retries, stop_on=(402,))
    if r is None:
        return None
    if r.status_code == 402:
        return QUOTA_EXHAUSTED  # free quota gone; do not retry
    return r.json()


def http_bytes(url: str, retries: int = 3):
    r = _http.get(url, headers=HEADERS, retries=retries, backoff=2.0,
                  max_backoff=6.0)
    return r.content if r is not None else None


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
    dest.parent.mkdir(parents=True, exist_ok=True)
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


# Bound to THIS module's INV/OUT_INV rather than imported outright: fetch_cmc
