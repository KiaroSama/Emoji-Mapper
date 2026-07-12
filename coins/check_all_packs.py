"""Final integrity check across ALL emoji packs.

Downloads every sticker from every pack, then reports:
  - BLANK stickers: fully transparent / effectively empty images
  - DUPLICATE stickers: distinct stickers (different custom_emoji_id) whose images
    are pixel-identical (same logo uploaded more than once)

Note: aliases that point multiple inventory tickers at ONE custom_emoji_id are NOT
duplicates -- that is a single shared sticker. Only distinct cids with identical
images count as duplicates here.

Resumable: per-sticker results are cached in pack_audit.json so a re-run after an
interruption (timeout / Telegram rate limit) continues instead of restarting.
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import hashlib
import io
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import requests
from PIL import Image

from build_pack import API_BASE, Telegram, load_env

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "rebuild_dedup_state.json"
AUDIT = ROOT / "pack_audit4.json"          # resume cache {cid: {hash, blank, set, pos}}
REPORT = ROOT / "pack_audit4_report.txt"

VISIBLE_ALPHA = 10   # alpha above this counts as a visible pixel
BLANK_MAX_PX = 8     # <= this many visible pixels => effectively blank


def load_audit() -> dict:
    if AUDIT.is_file():
        return json.loads(AUDIT.read_text("utf-8"))
    return {}


def save_audit(a: dict) -> None:
    AUDIT.write_text(json.dumps(a, ensure_ascii=False), "utf-8")


def analyze(data: bytes) -> tuple[str, bool]:
    """Return (image_hash, is_blank)."""
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    norm = im.resize((64, 64), Image.LANCZOS)
    h = hashlib.sha256(norm.tobytes()).hexdigest()[:24]
    alpha = im.split()[3]
    bbox = alpha.getbbox()
    if bbox is None:
        return h, True
    visible = sum(1 for p in alpha.get_flattened_data() if p > VISIBLE_ALPHA)
    return h, visible <= BLANK_MAX_PX


def main() -> int:
    load_env()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg = Telegram(token)
    sess = requests.Session()
    state = json.loads(STATE.read_text("utf-8"))
    sets = sorted(state["sets"], key=lambda x: x["index"])

    audit = load_audit()
    processed = 0
    for s in sets:
        name = s["name"]
        sticks = tg._call("getStickerSet", data={"name": name}).get("stickers", [])
        for pos, st in enumerate(sticks):
            cid = str(st.get("custom_emoji_id"))
            if cid in audit:
                continue
            fid = st["file_id"]
            data = None
            for attempt in range(1, 5):
                try:
                    fp = tg._call("getFile", data={"file_id": fid})["file_path"]
                    data = sess.get(f"{API_BASE}/file/bot{token}/{fp}", timeout=40).content
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  retry {attempt} set{s['index']} pos{pos}: {exc}", flush=True)
                    time.sleep(2 * attempt)
            if not data:
                print(f"  DOWNLOAD FAILED set{s['index']} pos{pos} cid{cid}", flush=True)
                continue
            try:
                h, blank = analyze(data)
            except Exception as exc:  # noqa: BLE001
                print(f"  ANALYZE FAILED set{s['index']} pos{pos}: {exc}", flush=True)
                h, blank = "ERROR", True
            audit[cid] = {"hash": h, "blank": blank, "set": s["index"], "pos": pos}
            processed += 1
            if processed % 100 == 0:
                save_audit(audit)
                print(f"  progress: {processed} new / {len(audit)} total", flush=True)
            time.sleep(0.03)
        print(f"set {s['index']} ({name}) done; audited so far: {len(audit)}", flush=True)
    save_audit(audit)

    # Build report.
    blanks = [(c, v) for c, v in audit.items() if v.get("blank")]
    by_hash: dict[str, list] = defaultdict(list)
    for c, v in audit.items():
        if v.get("hash") and v["hash"] != "ERROR":
            by_hash[v["hash"]].append((c, v))
    dup_groups = {h: items for h, items in by_hash.items() if len(items) > 1}

    lines = []
    lines.append(f"total stickers audited: {len(audit)}")
    lines.append(f"BLANK stickers: {len(blanks)}")
    for c, v in blanks:
        lines.append(f"  blank cid={c} set={v['set']} pos={v['pos']}")
    dup_count = sum(len(v) - 1 for v in dup_groups.values())
    lines.append(f"DUPLICATE image groups: {len(dup_groups)} "
                 f"(extra duplicate stickers: {dup_count})")
    for h, items in dup_groups.items():
        locs = ", ".join(f"set{v['set']}/pos{v['pos']}/cid{c}" for c, v in items)
        lines.append(f"  hash {h}: {locs}")
    report = "\n".join(lines)
    REPORT.write_text(report, encoding="utf-8")
    print("\n" + report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
