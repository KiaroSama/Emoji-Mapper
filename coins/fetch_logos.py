"""Fetch cryptocurrency logos.

Strategy:
- SVG (vector, colored) logos already live in ``logos/svg/<ticker>.svg`` (taken
  from open icon sets).
- For every other coin (by market cap, from CoinGecko) that has no SVG, download
  the raster PNG logo into ``logos/png/<ticker>.png``.
- Write ``keywords.csv`` (ticker, name, format, file, keywords) for every logo.

Robust by design: resumes (skips logos already on disk), sanitizes tickers into
Windows-safe filenames, and never lets one bad coin abort the whole run.

Downloads go to a temp file and are only published to their final name once they
decode as a real, non-empty image, so an error page or a truncated body can
never be cached as a logo.

Networking goes through coins/_http.py (one pooled requests.Session, shared
retry rules); Pillow is used only to validate downloaded images.
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared HTTP client whether it
# is run as ``python coins/fetch_logos.py`` or imported from the project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import argparse
import csv
import re
import time
from pathlib import Path

from PIL import Image

from coins import _http

ROOT = Path(__file__).resolve().parent
SVG_DIR = ROOT / "logos" / "svg"
PNG_DIR = ROOT / "logos" / "png"
KEYWORDS_CSV = ROOT / "keywords.csv"

API = "https://api.coingecko.com/api/v3/coins/markets"
PER_PAGE = 250
# 40 * 250 = up to 10,000 coins. A plain constant, resolved at import: this used
# to be int(sys.argv[1]), which made the module unimportable under any runner
# whose first argument is not a number -- `python coins/fetch_logos.py --help`
# raised ValueError before argparse could print anything. main() parses the
# override now, so the module can be imported without owning the command line.
MAX_PAGES = 40
PAGE_DELAY = _http.page_delay()   # COIN_PAGE_DELAY overrides; see coins/_http.py
IMG_DELAY = 0.05
HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher; local tool)"}

# Keep only Windows-safe filename characters.
_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def safe_ticker(symbol: str) -> str:
    return _SAFE_RE.sub("", str(symbol).lower().strip()).strip("._-")


def _get(url: str, *, binary: bool = False, retries: int = 6):
    # Long, escalating backoff so transient 429s don't end the run.
    resp = _http.get(url, headers=HEADERS, timeout=30, retries=retries,
                     backoff=8.0, max_backoff=60.0)
    if resp is None:
        raise RuntimeError(f"GET failed after {retries} attempts: {url}")
    return resp.content if binary else resp.json()


def _valid_image(path: Path) -> bool:
    """True if ``path`` decodes completely and has visible pixels.

    ``exists()`` proves nothing here: a rate-limit HTML body, a redirect page or
    a run killed mid-write all leave a file that the resume check would treat as
    a finished logo forever.
    """
    try:
        with Image.open(path) as im:
            im.verify()                     # full decode: catches truncation
        with Image.open(path) as im:        # verify() leaves the file unusable
            return im.convert("RGBA").getchannel("A").getbbox() is not None
    except Exception:  # noqa: BLE001 - anything unreadable is simply not a logo
        return False


def fetch_logo(url: str, dest: Path) -> bool:
    """Download ``url`` and publish it as ``dest`` only if it is a real image.

    Written to a sibling temp file and renamed (atomic on NTFS and POSIX), so a
    partial or non-image body never appears under the final name.
    """
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(_get(url, binary=True, retries=3))
        if not _valid_image(tmp):
            return False
        tmp.replace(dest)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def existing_svg_tickers() -> set[str]:
    return {p.stem.lower() for p in SVG_DIR.glob("*.svg")} if SVG_DIR.is_dir() else set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Download coin logos from CoinGecko and write keywords.csv.")
    # nargs="?" keeps the old positional form (`fetch_logos.py 5`) working, and
    # argparse rejects a non-numeric value with a usage error instead of the
    # ValueError the import-time int() used to raise.
    ap.add_argument("pages", nargs="?", type=int, default=None,
                    help=f"market-data pages of {PER_PAGE} coins "
                         f"(default {MAX_PAGES}).")
    args = ap.parse_args(argv)
    if args.pages is not None and args.pages < 1:
        ap.error("pages must be at least 1")
    # MAX_PAGES stays the default rather than the value: the tests patch it, and
    # a caller who passes nothing must get the module's documented default.
    pages = MAX_PAGES if args.pages is None else args.pages

    PNG_DIR.mkdir(parents=True, exist_ok=True)
    svg_tickers = existing_svg_tickers()
    print(f"SVG logos already present: {len(svg_tickers)}", flush=True)

    rows: dict[str, dict] = {}   # ticker -> keyword row (dedup by ticker)
    png_new = 0
    png_resumed = 0
    coins_seen = 0

    for page in range(1, pages + 1):
        url = (f"{API}?vs_currency=usd&order=market_cap_desc&per_page={PER_PAGE}"
               f"&page={page}&sparkline=false")
        print(f"[page {page}/{pages}] fetching market data...", flush=True)
        try:
            data = _get(url)
        except RuntimeError as exc:
            print(f"  stopping paging: {exc}", flush=True)
            break
        if not isinstance(data, list) or not data:
            print("  no more coins; done paging.", flush=True)
            break

        for coin in data:
            coins_seen += 1
            try:
                ticker = safe_ticker(coin.get("symbol", ""))
                name = str(coin.get("name", "")).strip()
                img = coin.get("image") or ""
                if not ticker or ticker in rows:
                    continue
                if ticker in svg_tickers:
                    rows[ticker] = {"ticker": ticker, "name": name, "format": "svg",
                                    "file": f"logos/svg/{ticker}.svg"}
                    continue
                dest = PNG_DIR / f"{ticker}.png"
                if dest.exists() and not _valid_image(dest):
                    # Cached garbage from an earlier run: drop it so the resume
                    # check below cannot keep serving it as this coin's logo.
                    print(f"  {ticker}: cached file is not an image; re-downloading",
                          flush=True)
                    dest.unlink(missing_ok=True)
                if dest.exists():  # resume: keep what we already downloaded
                    rows[ticker] = {"ticker": ticker, "name": name, "format": "png",
                                    "file": f"logos/png/{ticker}.png"}
                    png_resumed += 1
                    continue
                if not img or not str(img).startswith("http"):
                    continue
                if not fetch_logo(img, dest):
                    print(f"  skip {ticker}: download was not a usable image", flush=True)
                    continue
                rows[ticker] = {"ticker": ticker, "name": name, "format": "png",
                                "file": f"logos/png/{ticker}.png"}
                png_new += 1
                time.sleep(IMG_DELAY)
            except Exception as exc:  # noqa: BLE001 - never abort the whole run
                print(f"  skip coin {coin.get('id', '?')}: {exc}", flush=True)
                continue

        print(f"  page {page} done: seen={coins_seen} png_new={png_new} resumed={png_resumed}",
              flush=True)
        time.sleep(PAGE_DELAY)

    # Ensure every SVG logo is recorded even if not seen in the market pages.
    for t in sorted(svg_tickers):
        rows.setdefault(t, {"ticker": t, "name": "", "format": "svg",
                            "file": f"logos/svg/{t}.svg"})

    # Record every PNG already on disk, even from pages not reached this run --
    # but only after it decodes. These files were never checked here, so a
    # cached error page or a body truncated by an earlier run was advertised in
    # keywords.csv as that ticker's logo and fed straight into the pack.
    for p in sorted(PNG_DIR.glob("*.png")):
        t = p.stem.lower()
        if t in rows:
            continue          # already validated on the download/resume path
        if not _valid_image(p):
            print(f"  skip cached {p.name}: not a usable image", flush=True)
            continue
        rows[t] = {"ticker": t, "name": "", "format": "png",
                   "file": f"logos/png/{t}.png"}

    with open(KEYWORDS_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "name", "format", "file", "keywords"])
        for r in sorted(rows.values(), key=lambda x: x["ticker"]):
            kw = r["ticker"] if not r["name"] else f"{r['ticker']}, {r['name']}"
            w.writerow([r["ticker"], r["name"], r["format"], r["file"], kw])

    total_png = len({r["ticker"] for r in rows.values() if r["format"] == "png"})
    print("", flush=True)
    print(f"DONE: {len(svg_tickers)} SVG + {total_png} PNG = {len(svg_tickers) + total_png} "
          f"logos. (new png this run: {png_new}, resumed: {png_resumed}). keywords.csv written.",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
