"""Clean rebuild of the custom-emoji packs, duplicate-proof and exact.

Key idea: on every (re)start, how far we have progressed is read from the LIVE
state of Telegram (sum of stickers actually present in the sets), NOT from a
saved counter. So interrupting at any moment and resuming can never create a
duplicate: pending = sorted_tickers[total_live_stickers:].

Adds tickers in a fixed sorted order, 200 per set. Sends each finished pack's
share link (in order). When done, reads each set's custom_emoji_ids (live, in
order) -> exact ticker->id mapping -> fills currency-emoji-inventory.filled.md.

Usage:
  python rebuild_packs.py            delete old packs (first run) + build + map
  python rebuild_packs.py map        skip building; just map + fill from live
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from build_pack import Telegram, _sticker_json, load_env

ROOT = Path(__file__).resolve().parent
EMOJI = ROOT / "logos" / "emoji"
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"
STATE = ROOT / "rebuild_state.json"
OLD_STATE = ROOT / "pack_state.json"
BASE = "gvcryptoemoji"
TITLE = "@GodVerify Crypto Emoji"
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
load_env()  # ensure .env is loaded before resolving the owner id at import
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
USER_ID = int(os.environ.get("PACK_OWNER_USER_ID", "0"))


def sorted_tickers() -> list[str]:
    return [p.stem for p in sorted(EMOJI.glob("*.png")) if p.stat().st_size > 0]


def load_keywords() -> dict[str, str]:
    out: dict[str, str] = {}
    kp = ROOT / "keywords.csv"
    if kp.is_file():
        with open(kp, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out[row["ticker"].lower()] = row.get("keywords") or row["ticker"]
    return out


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"sets": [], "sent": []}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def delete_old_packs(tg: Telegram) -> None:
    if not OLD_STATE.is_file():
        return
    old = json.loads(OLD_STATE.read_text(encoding="utf-8"))
    for s in old.get("sets", []):
        try:
            tg._call("deleteStickerSet", data={"name": s["name"]})
            print(f"  deleted old pack {s['name']}", flush=True)
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            print(f"  (old pack {s['name']}: {exc})", flush=True)
    OLD_STATE.write_text(json.dumps({"sets": [], "done": [], "sent": []}), encoding="utf-8")


def live_count(tg: Telegram, name: str) -> int:
    try:
        return len(tg._call("getStickerSet", data={"name": name}).get("stickers", []))
    except Exception:  # noqa: BLE001
        return 0


def notify(tg: Telegram, state: dict, name: str, title: str) -> None:
    if name in state["sent"]:
        return
    try:
        tg.send_message(USER_ID, f"\u2705 {title}\nhttps://t.me/addemoji/{name}")
        state["sent"].append(name)
        save_state(state)
        print(f"  sent link for {name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  notify failed {name}: {exc}", flush=True)


def build(tg: Telegram) -> None:
    tickers = sorted_tickers()
    keywords = load_keywords()
    state = load_state()

    # First run: delete the old (damaged) packs and start clean.
    if not state["sets"] and not state.get("deleted_old"):
        print("deleting old packs...", flush=True)
        delete_old_packs(tg)
        state["deleted_old"] = True
        save_state(state)

    # Reconcile progress from LIVE Telegram counts (duplicate-proof).
    cum = 0
    for s in state["sets"]:
        s["live"] = live_count(tg, s["name"])
        cum += s["live"]
    print(f"resume: {len(state['sets'])} sets, {cum} live stickers, "
          f"{len(tickers) - cum} pending", flush=True)

    if state["sets"] and state["sets"][-1]["live"] < PER_SET:
        cur = state["sets"][-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index, set_name, in_set = len(state["sets"]), "", 0

    for ticker in tickers[cum:]:
        png = EMOJI / f"{ticker}.png"
        if not png.is_file() or png.stat().st_size == 0:
            continue
        kw = keywords.get(ticker, ticker)
        try:
            placed = False
            if in_set != 0:
                try:
                    tg.add_sticker(USER_ID, set_name, png, EMOJI_CHAR, kw)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                set_index += 1
                set_name = f"{BASE}{set_index}_by_{tg_username}"
                title = f"{TITLE} {set_index}"
                tg.create_set(USER_ID, set_name, title, png, EMOJI_CHAR, kw)
                state["sets"].append({"index": set_index, "name": set_name, "title": title})
                save_state(state)
                print(f"[set {set_index}] created {set_name}", flush=True)
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            print(f"  skip {ticker}: {exc}", flush=True)
            continue
        in_set += 1
        if in_set >= PER_SET:
            notify(tg, state, set_name, f"{TITLE} {set_index}")
            in_set = 0
        time.sleep(0.1)

    # Final partial set finished -> send its link.
    if state["sets"]:
        last = state["sets"][-1]
        notify(tg, state, last["name"], last["title"])


def map_and_fill(tg: Telegram) -> None:
    tickers = sorted_tickers()
    state = load_state()
    ticker_to_id: dict[str, str] = {}
    cum = 0
    for s in sorted(state["sets"], key=lambda x: x["index"]):
        cids = [str(st.get("custom_emoji_id", ""))
                for st in tg._call("getStickerSet", data={"name": s["name"]}).get("stickers", [])]
        for i, cid in enumerate(cids):
            if cum + i < len(tickers):
                ticker_to_id[tickers[cum + i]] = cid
        cum += len(cids)
    (ROOT / "ticker_to_id.json").write_text(
        json.dumps(ticker_to_id, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"mapped {len(ticker_to_id)} tickers; total live {cum}", flush=True)
    fill_inventory(ticker_to_id)


def fill_inventory(ticker_to_id: dict[str, str]) -> None:
    lines = INV.read_text(encoding="utf-8").split("\n")
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
    print(f"inventory filled: {filled}/{total} -> {OUT_INV.name}", flush=True)


tg_username = ""

if __name__ == "__main__":
    load_env()
    _tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    tg_username = _tg.get_me()["username"]
    if len(sys.argv) > 1 and sys.argv[1] == "map":
        map_and_fill(_tg)
    else:
        build(_tg)
        map_and_fill(_tg)
