"""Review (and precisely fix) coin logos against official CoinGecko art.

Coin tickers are shared by many tokens, so a logo fetched by symbol can be the
wrong coin (e.g. a green "SOL" token instead of Solana). This tool pulls the
top-N coins by market cap, compares each one's official logo to the local source
logo (``<symbol>.png``) with a perceptual hash, and lists candidates that differ.

IMPORTANT: a large perceptual distance does NOT prove a logo is wrong. Many
correct logos come from different icon sets (different art) than CoinGecko, so
they differ too. Treat the report as a REVIEW list for a human, not ground
truth. Fixing is therefore restricted to an explicit, user-confirmed ticker list
(``--fix --only sym1,sym2``) so correct logos are never clobbered.

Usage:
  python coins/verify_logos.py --emoji-dir "PATH/emoji" [--top 100]
  python coins/verify_logos.py --emoji-dir "PATH/emoji" --fix --only sol,xrp
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import io
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

import requests
from PIL import Image

from build_pack import API_BASE, Telegram, load_env, _input_sticker, _mime_for_path
from emojikit import media
from emojikit.media import _dhash, hamming
from emojikit.logsetup import setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("verify_logos")
UA = {"User-Agent": "Mozilla/5.0 (logo-verify; local tool)"}


def dh(img: Image.Image) -> int:
    return _dhash(img.convert("RGBA"))


def fetch_markets(top: int) -> list[dict]:
    out: list[dict] = []
    per = 250
    for page in range(1, (top + per - 1) // per + 1):
        url = (f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
               f"&order=market_cap_desc&per_page={per}&page={page}&sparkline=false")
        req = urllib.request.Request(url, headers=UA)
        out.extend(json.loads(urllib.request.urlopen(req, timeout=60).read()))
        time.sleep(3)
    return out[:top]


def fetch_image(url: str) -> bytes | None:
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read()
    except Exception as exc:  # noqa: BLE001
        log.debug("image fetch failed %s: %s", url, exc)
        return None


def report(emoji_dir: Path, top: int, threshold: int) -> list[tuple[int, str, str, str]]:
    coins = fetch_markets(top)
    log.info("fetched %d market coins", len(coins))
    flagged: list[tuple[int, str, str, str]] = []
    for c in coins:
        sym = str(c.get("symbol", "")).lower()
        local = emoji_dir / f"{sym}.png"
        if not sym or not local.is_file():
            continue
        data = fetch_image(c.get("image", ""))
        if not data:
            continue
        try:
            d = hamming(dh(Image.open(io.BytesIO(data))), dh(Image.open(local)))
        except Exception:  # noqa: BLE001
            continue
        if d > threshold:
            flagged.append((d, sym, str(c.get("name", "")), str(c.get("image", ""))))
        time.sleep(0.05)
    flagged.sort(reverse=True)
    log.info("flagged %d / %d coins (distance > %d) -- REVIEW ONLY, not proof of error:",
             len(flagged), len(coins), threshold)
    for d, sym, name, _ in flagged:
        log.info("  %-8s d=%-4d %s", sym, d, name)
    return flagged


def cid_location(tg: Telegram, sets: list[dict], cid: str) -> tuple[str, int, str] | None:
    """Return (set_name, position, file_id) of a custom_emoji_id, or None."""
    for s in sets:
        sticks = tg.get_sticker_set(s["name"]).get("stickers", [])
        for pos, st in enumerate(sticks):
            if str(st.get("custom_emoji_id")) == str(cid):
                return s["name"], pos, st["file_id"]
    return None


def fix_one(tg: Telegram, uid: int, sets: list[dict], mp: dict, emoji_dir: Path,
            sym: str) -> bool:
    """Replace one ticker's sticker with the official CoinGecko logo."""
    # Resolve official image for this exact symbol via market lookup.
    coins = fetch_markets(250)
    coin = next((c for c in coins if str(c.get("symbol", "")).lower() == sym), None)
    if not coin:
        log.warning("%s: not found in top markets; skip", sym)
        return False
    data = fetch_image(coin["image"])
    if not data:
        log.warning("%s: official image fetch failed; skip", sym)
        return False

    old_cid = str(mp.get(sym, ""))
    loc = cid_location(tg, sets, old_cid)
    if not loc:
        log.warning("%s: current cid %s not found live; skip", sym, old_cid)
        return False
    sname, pos, old_fid = loc

    # Update local source files (full-res + 100x100) and upload the replacement.
    png_dir = emoji_dir.parent / "png"
    png_dir.mkdir(exist_ok=True)
    (png_dir / f"{sym}.png").write_bytes(data)
    tmp = ROOT / "_vtmp.png"
    tmp.write_bytes(data)
    src = emoji_dir / f"{sym}.png"
    media.to_static_png(tmp, src)
    tmp.unlink(missing_ok=True)

    with open(src, "rb") as fh:
        tg._call("replaceStickerInSet", data={
            "user_id": uid, "name": sname, "old_sticker": old_fid,
            "sticker": json.dumps(_input_sticker("static", ["\U0001FA99"],
                                                 [sym, str(coin.get("name", "")).lower()])),
        }, files={"file0": (src.name, fh, _mime_for_path(src))})

    # The replacement keeps its position; read the new cid there.
    live = tg.get_sticker_set(sname).get("stickers", [])
    new_cid = str(live[pos].get("custom_emoji_id")) if pos < len(live) else None
    if not new_cid:
        log.warning("%s: could not read new cid", sym)
        return False
    changed = 0
    for t, c in list(mp.items()):
        if str(c) == old_cid:
            mp[t] = new_cid
            changed += 1
    log.info("fixed %s: %s -> %s (%d map entries)", sym, old_cid, new_cid, changed)
    return True


def main() -> int:
    load_env()
    setup_logging("verify_logos")
    ap = argparse.ArgumentParser()
    ap.add_argument("--emoji-dir", required=True)
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--map", default=str(ROOT / "ticker_to_id.json"))
    ap.add_argument("--state", default=str(ROOT / "rebuild_dedup_state.json"))
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--threshold", type=int, default=20, help="Review-flag distance.")
    ap.add_argument("--fix", action="store_true", help="Fix the --only tickers.")
    ap.add_argument("--only", default="", help="Comma-separated tickers to fix (required with --fix).")
    args = ap.parse_args()

    emoji_dir = Path(args.emoji_dir)
    if not emoji_dir.is_dir():
        log.error("emoji dir not found: %s", emoji_dir)
        return 2

    if not args.fix:
        report(emoji_dir, args.top, args.threshold)
        log.info("Review only. To repair confirmed-wrong logos: --fix --only sol,xrp")
        return 0

    syms = [s.strip().lower() for s in args.only.split(",") if s.strip()]
    if not syms:
        log.error("--fix requires --only with an explicit ticker list "
                  "(automated detection is not reliable enough to mass-fix).")
        return 2

    tg = Telegram(os.environ[args.token_env])
    uid = int(os.environ["PACK_OWNER_USER_ID"])
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    sets = sorted(state["sets"], key=lambda s: s["index"])
    mp = json.loads(Path(args.map).read_text(encoding="utf-8"))

    fixed = 0
    for sym in syms:
        if fix_one(tg, uid, sets, mp, emoji_dir, sym):
            fixed += 1
            Path(args.map).write_text(json.dumps(mp, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
        time.sleep(0.3)
    log.info("fixed %d/%d requested logos; map saved.", fixed, len(syms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
