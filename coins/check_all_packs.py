"""Final integrity check across ALL emoji packs.

Downloads every sticker from every pack, then reports:
  - BLANK stickers: fully transparent / effectively empty images
  - DUPLICATE stickers: distinct stickers (different custom_emoji_id) whose images
    are pixel-identical (same logo uploaded more than once)

Note: aliases that point multiple inventory tickers at ONE custom_emoji_id are NOT
duplicates -- that is a single shared sticker. Only distinct cids with identical
images count as duplicates here.

Resumable: per-sticker results are cached in pack_audit.json so a re-run after an
interruption (timeout / Telegram rate limit) continues instead of restarting. The
report is always derived from the CURRENT live manifest, so a deleted sticker
leaves the totals and duplicate groups, and a sticker that failed to download or
decode is recorded as an error and retried on the next run instead of being
cached forever as "blank".
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

from build_pack import (EXIT_USAGE, Telegram, api_base, ingest_exit_code, load_env,
                        write_json_atomic)

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "rebuild_dedup_state.json"
AUDIT = ROOT / "pack_audit4.json"          # resume cache {cid: {hash, blank} | {error}}
REPORT = ROOT / "pack_audit4_report.txt"

VISIBLE_ALPHA = 10   # alpha above this counts as a visible pixel
BLANK_MAX_PX = 8     # <= this many visible pixels => effectively blank


def load_audit() -> dict:
    if AUDIT.is_file():
        return json.loads(AUDIT.read_text("utf-8"))
    return {}


def save_audit(a: dict) -> None:
    write_json_atomic(AUDIT, a)


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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set.", flush=True)
        return EXIT_USAGE
    tg = Telegram(token)
    sess = requests.Session()
    state = json.loads(STATE.read_text("utf-8"))
    sets = sorted(state["sets"], key=lambda x: x["index"])

    audit = load_audit()
    live: dict[str, dict] = {}   # cid -> {set, pos} from the CURRENT manifest
    processed = 0
    for s in sets:
        name = s["name"]
        sticks = tg.get_sticker_set(name).get("stickers", [])
        for pos, st in enumerate(sticks):
            cid = str(st.get("custom_emoji_id"))
            # Positions shift whenever a sticker is added or removed, so the
            # location always comes from the live manifest, never from cache.
            live[cid] = {"set": s["index"], "pos": pos}
            cached = audit.get(cid)
            if cached and not cached.get("error"):
                continue          # only a successful analysis is final
            data, err = None, ""
            for attempt in range(1, 5):
                try:
                    fp = tg._call("getFile", data={"file_id": st["file_id"]})["file_path"]
                    r = sess.get(f"{api_base()}/file/bot{token}/{fp}", timeout=40)
                    r.raise_for_status()  # an error page is not an image
                    data = r.content
                    break
                except Exception as exc:  # noqa: BLE001
                    # _safe strips the bot token that the file URL embeds.
                    err = tg._safe(exc)
                    print(f"  retry {attempt} set{s['index']} pos{pos}: {err}", flush=True)
                    time.sleep(2 * attempt)
            try:
                h, blank = analyze(data) if data else ("", None)
            except Exception as exc:  # noqa: BLE001
                err = tg._safe(exc)
                print(f"  ANALYZE FAILED set{s['index']} pos{pos}: {err}", flush=True)
                h, blank = "", None
            if blank is None:
                # An unreadable sticker is an ERROR, not a blank one: recording
                # it as blank=true made every re-run skip it and report a coin
                # that is perfectly fine as missing artwork.
                audit[cid] = {"error": err or "download failed"}
                print(f"  FAILED set{s['index']} pos{pos} cid{cid}", flush=True)
            else:
                audit[cid] = {"hash": h, "blank": blank}
            processed += 1
            if processed % 100 == 0:
                save_audit(audit)
                print(f"  progress: {processed} new / {len(audit)} total", flush=True)
            time.sleep(0.03)
        print(f"set {s['index']} ({name}) done; audited so far: {len(audit)}", flush=True)

    # Reconcile: keep only stickers that are still live, so deleted ones drop out
    # of the totals, the blank list and the duplicate groups.
    audit = {c: v for c, v in audit.items() if c in live}
    save_audit(audit)

    ok = {c: v for c, v in audit.items() if not v.get("error")}
    errors = [c for c in live if c not in ok]
    blanks = [(c, v) for c, v in ok.items() if v.get("blank")]
    by_hash: dict[str, list] = defaultdict(list)
    for c, v in ok.items():
        if v.get("hash"):
            by_hash[v["hash"]].append(c)
    dup_groups = {h: cs for h, cs in by_hash.items() if len(cs) > 1}

    lines = []
    lines.append(f"live stickers: {len(live)}")
    lines.append(f"analysed: {len(ok)}")
    lines.append(f"FAILED (retried on next run): {len(errors)}")
    for c in errors:
        lines.append(f"  failed cid={c} set={live[c]['set']} pos={live[c]['pos']} "
                     f"({audit.get(c, {}).get('error', 'not analysed')})")
    lines.append(f"BLANK stickers: {len(blanks)}")
    for c, v in blanks:
        lines.append(f"  blank cid={c} set={live[c]['set']} pos={live[c]['pos']}")
    dup_count = sum(len(cs) - 1 for cs in dup_groups.values())
    lines.append(f"DUPLICATE image groups: {len(dup_groups)} "
                 f"(extra duplicate stickers: {dup_count})")
    for h, cs in dup_groups.items():
        locs = ", ".join(f"set{live[c]['set']}/pos{live[c]['pos']}/cid{c}" for c in cs)
        lines.append(f"  hash {h}: {locs}")
    report = "\n".join(lines)
    REPORT.write_text(report, encoding="utf-8")
    print("\n" + report, flush=True)
    return ingest_exit_code(len(ok), len(errors))


if __name__ == "__main__":
    raise SystemExit(main())
