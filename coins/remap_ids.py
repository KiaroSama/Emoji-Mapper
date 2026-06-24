"""Rebuild ticker_to_id.json by matching live pack images to source logos.

Why this exists: the original mapping was built from the dedup *plan order*, but
the live packs contain images in a slightly different order (some plan items were
skipped/failed during upload, shifting every later position). As a result the
position-based ticker->custom_emoji_id table is misaligned from the first gap
onward, so many tickers point at the wrong sticker.

This tool ignores position entirely and re-derives the mapping from image
content: it downloads every live sticker once, computes a small perceptual
signature, and matches each local source logo (``<ticker>.png``) to the live
sticker whose image is most similar. The result is a corrected, content-based
ticker -> custom_emoji_id map. It NEVER modifies the Telegram packs.

Usage:
  python coins/remap_ids.py --emoji-dir "PATH/TO/emoji" [--apply]

Resumable: live signatures are cached in remap_live_cache.json, so an
interrupted run continues without re-downloading.
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import base64
import io
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from build_pack import API_BASE, Telegram, load_env
from emojikit.logsetup import setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("remap_ids")
SIG_PX = 16  # signature is a SIG_PX x SIG_PX RGB thumbnail (robust to re-encode)


def signature(img: Image.Image) -> np.ndarray:
    """Small RGB thumbnail signature, alpha composited on black for consistency."""
    im = img.convert("RGBA")
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    im = Image.alpha_composite(bg, im).convert("RGB").resize((SIG_PX, SIG_PX), Image.LANCZOS)
    return np.frombuffer(im.tobytes(), dtype=np.uint8).astype(np.float32)


def load_cache(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done_sets": [], "sigs": {}}  # sigs: cid -> base64(768 bytes)


def save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache), encoding="utf-8")


def download_live(tg: Telegram, token: str, sets: list[dict], cache: dict,
                  cache_path: Path) -> None:
    """Download every live sticker once and cache its signature (resumable)."""
    sess = requests.Session()
    done = set(cache["done_sets"])
    for s in sets:
        if s["index"] in done:
            continue
        ss = tg.get_sticker_set(s["name"])
        sticks = ss.get("stickers", [])
        for pos, st in enumerate(sticks):
            cid = str(st.get("custom_emoji_id"))
            if cid in cache["sigs"]:
                continue
            try:
                fp = tg._call("getFile", data={"file_id": st["file_id"]})["file_path"]
                data = sess.get(f"{API_BASE}/file/bot{token}/{fp}", timeout=40).content
                sig = signature(Image.open(io.BytesIO(data)))
                cache["sigs"][cid] = base64.b64encode(sig.astype(np.uint8).tobytes()).decode()
            except Exception as exc:  # noqa: BLE001
                log.warning("set %d pos %d cid %s failed: %s", s["index"], pos, cid, exc)
            time.sleep(0.02)
        cache["done_sets"].append(s["index"])
        save_cache(cache_path, cache)
        log.info("set %d (%s): cached %d live signatures total",
                 s["index"], s["name"], len(cache["sigs"]))


def build_local(emoji_dir: Path) -> tuple[list[str], np.ndarray]:
    """Compute signatures for every local <ticker>.png source logo."""
    tickers, sigs = [], []
    for p in sorted(emoji_dir.glob("*.png")):
        try:
            sigs.append(signature(Image.open(p)))
            tickers.append(p.stem.lower())
        except Exception as exc:  # noqa: BLE001
            log.warning("local %s failed: %s", p.name, exc)
    return tickers, np.array(sigs, dtype=np.float32)


def nearest(local: np.ndarray, live: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each local row, index of nearest live row + squared L2 distance."""
    ln2 = (local * local).sum(1)
    vn2 = (live * live).sum(1)
    out_idx = np.empty(local.shape[0], dtype=np.int64)
    out_d2 = np.empty(local.shape[0], dtype=np.float64)
    # Chunk over local rows to keep the Gram matrix memory bounded.
    step = 512
    for i in range(0, local.shape[0], step):
        block = local[i:i + step]
        g = block @ live.T
        d2 = ln2[i:i + step, None] - 2 * g + vn2[None, :]
        out_idx[i:i + step] = d2.argmin(1)
        out_d2[i:i + step] = d2[np.arange(block.shape[0]), out_idx[i:i + step]]
    return out_idx, out_d2


def main() -> int:
    load_env()
    setup_logging("remap_ids")
    ap = argparse.ArgumentParser()
    ap.add_argument("--emoji-dir", required=True, help="Folder of <ticker>.png source logos.")
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--state", default=str(ROOT / "rebuild_dedup_state.json"))
    ap.add_argument("--cache", default=str(ROOT / "remap_live_cache.json"))
    ap.add_argument("--out", default=str(ROOT / "ticker_to_id.json"))
    ap.add_argument("--max-distance", type=float, default=0,
                    help="Omit tickers whose best image match exceeds this distance "
                         "(0 = keep all). Use to drop coins that were never uploaded.")
    ap.add_argument("--apply", action="store_true", help="Write the corrected map.")
    args = ap.parse_args()

    emoji_dir = Path(args.emoji_dir)
    if not emoji_dir.is_dir():
        log.error("emoji dir not found: %s", emoji_dir)
        return 2
    token = os.environ.get(args.token_env, "")
    if not token:
        log.error("%s not set.", args.token_env)
        return 2

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    sets = sorted(state["sets"], key=lambda s: s["index"])
    tg = Telegram(token)
    log.info("bot @%s; %d sets", tg.get_me()["username"], len(sets))

    cache_path = Path(args.cache)
    cache = load_cache(cache_path)
    download_live(tg, token, sets, cache, cache_path)

    cids = list(cache["sigs"].keys())
    live = np.array([np.frombuffer(base64.b64decode(cache["sigs"][c]), dtype=np.uint8)
                     for c in cids], dtype=np.float32)
    tickers, local = build_local(emoji_dir)
    log.info("matching %d local logos against %d live stickers ...", len(tickers), len(cids))
    idx, d2 = nearest(local, live)

    new_map: dict[str, str] = {}
    dists = []
    for t, j, dd in zip(tickers, idx, d2):
        new_map[t] = cids[j]
        dists.append(dd ** 0.5)
    dists = np.array(dists)

    # Distance distribution helps choose a cutoff that separates real matches
    # (deduped siblings included) from coins that were never uploaded.
    for thr in (50, 100, 150, 200, 300, 500):
        log.info("  matches with distance > %d: %d", thr, int((dists > thr).sum()))

    # Drop poor matches (no genuine live image) instead of mis-mapping them.
    omitted = 0
    if args.max_distance > 0:
        for t, d in list(zip(tickers, dists)):
            if d > args.max_distance:
                new_map.pop(t, None)
                omitted += 1
        log.info("omitted %d tickers with distance > %d (no reliable live match)",
                 omitted, args.max_distance)

    # Compare against the existing (broken) map.
    old = {}
    outp = Path(args.out)
    if outp.is_file():
        old = json.loads(outp.read_text(encoding="utf-8"))
    changed = sum(1 for t, c in new_map.items() if old.get(t) != c)
    log.info("rebuilt map: %d tickers | changed vs current: %d", len(new_map), changed)
    log.info("match distance: min=%.1f median=%.1f p95=%.1f max=%.1f",
             dists.min(), np.median(dists), np.percentile(dists, 95), dists.max())
    worst = sorted(zip(tickers, dists), key=lambda x: -x[1])[:10]
    log.info("worst matches (review): %s", [(t, round(float(d))) for t, d in worst])
    for probe in ("btc", "eth", "usdt", "usdu", "sol", "xrp"):
        if probe in new_map:
            log.info("  %s -> %s (was %s)%s", probe, new_map[probe],
                     old.get(probe), "" if new_map[probe] != old.get(probe) else " [unchanged]")

    if args.apply:
        bak = outp.with_suffix(".prebroken.json")
        if outp.is_file() and not bak.exists():
            bak.write_text(outp.read_text(encoding="utf-8"), encoding="utf-8")
            log.info("backed up old map -> %s", bak.name)
        outp.write_text(json.dumps(new_map, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("WROTE corrected map -> %s", outp)
    else:
        log.info("dry run (no file written). Re-run with --apply to save.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
