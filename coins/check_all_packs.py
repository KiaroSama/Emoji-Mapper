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
import os as _bootstrap_os
import sys as _bootstrap_sys
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

from build_pack import (EXIT_FAILED, EXIT_USAGE, ingest_exit_code, load_env)
from packstate import (write_json_atomic)
from telegram_api import (Telegram, api_base)
# The pipeline's single definition of "this image is effectively empty" -- this
# module used to carry its own copy of the rule and its two constants.
from emojikit.media import is_blank_image

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "rebuild_dedup_state.json"
AUDIT = ROOT / "pack_audit4.json"          # resume cache {cid: {hash, blank} | {error}}
REPORT = ROOT / "pack_audit4_report.txt"


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
    return h, is_blank_image(im)


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
        # The set LISTING is the call most likely to be throttled when walking
        # many sets, and it had no retry at all while the download below had
        # four: one 429 here killed the run and threw away every sticker
        # analysed since the last 100-sticker checkpoint.
        sticks = None
        for attempt in range(1, 5):
            try:
                sticks = tg.get_sticker_set(name).get("stickers", [])
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  retry {attempt} set{s['index']} listing: {tg._safe(exc)}",
                      flush=True)
                time.sleep(2 * attempt)
        if sticks is None:
            # Stopping is the only honest option: `live` would be missing this
            # whole set, so the reconcile below would drop every cached analysis
            # in it and the report would call its coins deleted.
            save_audit(audit)
            print(f"ABORT: set {s['index']} ({name}) could not be listed; "
                  f"{len(audit)} analysed stickers kept for the next run.",
                  flush=True)
            return EXIT_FAILED
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
        # Checkpoint at the set boundary too: the 100-sticker counter can leave
        # up to 99 analysed stickers unsaved, and re-downloading them is exactly
        # what makes the next rate-limit likelier.
        save_audit(audit)
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
    for c, _v in blanks:
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
