"""Full, deduplicated rebuild of all custom-emoji packs (Option B).

Deletes every existing pack and rebuilds from scratch so that:
  - NO two stickers share the same image (deduplicated by image content), and
  - every coin that shares a logo with others is still represented: all its
    tickers map to the single shared sticker, and that sticker's keywords merge
    the coins' tickers/names (up to Telegram's keyword budget); the full coin
    list per shared image is saved to shared_logo_groups.json.

Design:
  - A FROZEN plan (rebuild_dedup_plan.json) lists one representative image per
    distinct image hash, in a fixed order. This is the canonical upload list, so
    resume is deterministic and can never create a duplicate.
  - Progress is reconciled from LIVE Telegram counts (sum of stickers actually
    present), exactly like rebuild_packs.py -> duplicate-proof across restarts.
  - New pack base name 'cemoji' (the old 'cryptoemoji' names are being deleted;
    a fresh base avoids name-reuse conflicts). Titles: '@YourBrand Crypto Emoji N'.

Usage:
  python rebuild_dedup.py            # build plan (if needed) + delete old + build + map
  python rebuild_dedup.py map        # skip building; map live cids + fill inventory
  python rebuild_dedup.py links      # (re)send the final combined links message
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image

from build_pack import Telegram, load_env

ROOT = Path(__file__).resolve().parent
EMOJI = ROOT / "logos" / "emoji"
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"
OLD_STATE = ROOT / "rebuild_state.json"        # the current 30 packs, to delete
PLAN = ROOT / "rebuild_dedup_plan.json"
STATE = ROOT / "rebuild_dedup_state.json"
GROUPS_REPORT = ROOT / "shared_logo_groups.json"
TICKER_IDS = ROOT / "ticker_to_id.json"
KEYWORDS_CSV = ROOT / "keywords.csv"

BASE = "cryptoemoji"
TITLE = "@YourBrand Crypto Emoji"
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
load_env()  # ensure .env is loaded before resolving the owner id at import
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
USER_ID = int(os.environ.get("PACK_OWNER_USER_ID", "0"))


def img_hash(path: Path) -> str:
    im = Image.open(path).convert("RGBA").resize((64, 64), Image.LANCZOS)
    return hashlib.sha256(im.tobytes()).hexdigest()[:24]


def is_blank(path: Path, min_visible: int = 8) -> bool:
    """True if an image is effectively empty -- never upload a blank emoji."""
    try:
        alpha = Image.open(path).convert("RGBA").split()[3]
    except Exception:  # noqa: BLE001
        return True
    if alpha.getbbox() is None:
        return True
    return sum(1 for v in alpha.getdata() if v > 10) <= min_visible


def load_keywords() -> dict[str, str]:
    out: dict[str, str] = {}
    if KEYWORDS_CSV.is_file():
        with open(KEYWORDS_CSV, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out[row["ticker"].lower()] = row.get("keywords") or row["ticker"]
    return out


def inventory_tickers() -> set[str]:
    txt = INV.read_text(encoding="utf-8")
    return {t.strip().lower() for t in re.findall(r"ticker:\s*(\S+)", txt)}


def build_plan() -> list[dict]:
    """Group local emoji PNGs by image hash; one representative per group.

    Representative preference: an inventory ticker if the group has one, else the
    alphabetically-first ticker. Keywords merge all tickers in the group.
    """
    keywords = load_keywords()
    inv = inventory_tickers()
    files = [p for p in sorted(EMOJI.glob("*.png")) if p.stat().st_size > 0]
    hash_to_tickers: dict[str, list[str]] = defaultdict(list)
    for p in files:
        try:
            hash_to_tickers[img_hash(p)].append(p.stem.lower())
        except Exception as exc:  # noqa: BLE001
            print(f"  hash failed {p.name}: {exc}", flush=True)

    groups: list[dict] = []
    for h, tickers in hash_to_tickers.items():
        ts = sorted(set(tickers))
        rep = next((t for t in ts if t in inv), ts[0])
        ordered = [rep] + [t for t in ts if t != rep]
        kw = ", ".join(keywords.get(t, t) for t in ordered)
        groups.append({"rep": rep, "tickers": ordered, "kw": kw, "hash": h})
    groups.sort(key=lambda g: g["rep"])  # deterministic, frozen order

    PLAN.write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
    # Documentation: only the shared-logo groups (>1 coin per image).
    shared = {g["rep"]: g["tickers"] for g in groups if len(g["tickers"]) > 1}
    GROUPS_REPORT.write_text(json.dumps(shared, ensure_ascii=False, indent=1), encoding="utf-8")
    dup_extra = sum(len(g["tickers"]) - 1 for g in groups)
    print(f"plan: {len(groups)} unique images | shared-logo groups: {len(shared)} "
          f"| coins collapsing onto a shared image: {dup_extra}", flush=True)
    return groups


def load_plan() -> list[dict]:
    if PLAN.is_file():
        return json.loads(PLAN.read_text(encoding="utf-8"))
    return build_plan()


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"sets": [], "sent": [], "deleted_old": False, "final_sent": False}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def live_count(tg: Telegram, name: str) -> int:
    try:
        return len(tg._call("getStickerSet", data={"name": name}).get("stickers", []))
    except Exception:  # noqa: BLE001
        return 0


def msg(tg: Telegram, text: str) -> None:
    """Send a message to the owner with link previews disabled."""
    tg._call("sendMessage", data={
        "chat_id": USER_ID, "text": text, "disable_web_page_preview": True,
    })


def notify(tg: Telegram, state: dict, name: str, title: str) -> None:
    if name in state["sent"]:
        return
    try:
        msg(tg, f"\u2705 {title}\nhttps://t.me/addemoji/{name}")
        state["sent"].append(name)
        save_state(state)
        print(f"  sent link for {name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  notify failed {name}: {exc}", flush=True)


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


def build(tg: Telegram, bot: str) -> None:
    plan = load_plan()
    state = load_state()
    state.setdefault("order", [])  # actual successful-upload order (drift-proof map)

    if not state.get("deleted_old"):
        print("deleting ALL old packs (full rebuild)...", flush=True)
        delete_old_packs(tg)
        state["deleted_old"] = True
        save_state(state)

    # Reconcile progress from LIVE counts -> duplicate-proof.
    cum = 0
    for s in state["sets"]:
        s["live"] = live_count(tg, s["name"])
        cum += s["live"]
    print(f"resume: {len(state['sets'])} sets, {cum} live stickers, "
          f"{len(plan) - cum} pending", flush=True)

    if state["sets"] and state["sets"][-1]["live"] < PER_SET:
        cur = state["sets"][-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index, set_name, in_set = len(state["sets"]), "", 0

    for g in plan[cum:]:
        png = EMOJI / f"{g['rep']}.png"
        if not png.is_file() or png.stat().st_size == 0:
            print(f"  skip {g['rep']}: missing/empty", flush=True)
            continue
        if is_blank(png):
            print(f"  skip {g['rep']}: blank image (no blank emoji)", flush=True)
            continue
        kw = g["kw"]
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
                set_name = f"{BASE}{set_index}_by_{bot}"
                title = f"{TITLE} {set_index}"
                tg.create_set(USER_ID, set_name, title, png, EMOJI_CHAR, kw)
                state["sets"].append({"index": set_index, "name": set_name,
                                      "title": title})
                save_state(state)
                print(f"[set {set_index}] created {set_name}", flush=True)
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            print(f"  skip {g['rep']}: {exc}", flush=True)
            continue
        in_set += 1
        state["order"].append(g["rep"])  # record actual upload order
        if in_set >= PER_SET:
            notify(tg, state, set_name, f"{TITLE} {set_index}")
            in_set = 0
        time.sleep(0.1)

    if state["sets"]:
        last = state["sets"][-1]
        notify(tg, state, last["name"], last["title"])


BACKUP_IDS = ROOT / "ticker_to_id.bak.json"


def reapply_aliases(new_map: dict[str, str]) -> int:
    """Re-apply chain/name aliases that have no own PNG (e.g. bnbbsc->bnb).

    The old ticker_to_id (backup) recorded these aliases by pointing the alias
    ticker at the SAME cid as its base coin. After the rebuild the base coin gets
    a NEW cid; map every PNG-less alias onto a sibling that IS in the new map.
    """
    if not BACKUP_IDS.is_file():
        print("  no backup map; skipping alias re-apply.", flush=True)
        return 0
    old = json.loads(BACKUP_IDS.read_text(encoding="utf-8"))
    old_groups: dict[str, list[str]] = defaultdict(list)
    for t, c in old.items():
        old_groups[str(c)].append(t)
    added = 0
    for t, c in old.items():
        if t in new_map:
            continue
        for sib in old_groups.get(str(c), []):
            if sib in new_map:
                new_map[t] = new_map[sib]
                added += 1
                break
    print(f"  re-applied {added} aliases from backup.", flush=True)
    return added


def map_and_fill(tg: Telegram) -> None:
    plan = load_plan()
    state = load_state()
    sets = sorted(state["sets"], key=lambda x: x["index"])
    cids: list[str] = []
    for s in sets:
        cids += [str(st.get("custom_emoji_id", ""))
                 for st in tg._call("getStickerSet", data={"name": s["name"]}).get("stickers", [])]

    ticker_to_id: dict[str, str] = {}
    order = state.get("order") or []
    if len(order) == len(cids) and order:
        # Preferred, drift-proof: map cids to the ACTUAL upload order recorded
        # during build (immune to skipped/failed items shifting positions).
        by_rep = {g["rep"]: g for g in plan}
        for i, cid in enumerate(cids):
            g = by_rep.get(order[i])
            if not g:
                continue
            for t in g["tickers"]:
                ticker_to_id[t] = cid
        print(f"mapped via recorded upload order ({len(cids)} stickers)", flush=True)
    else:
        # Fallback (legacy): assumes live order == plan order. If this warns, run
        # coins/remap_ids.py to rebuild the map from image content instead.
        if len(cids) != len(plan):
            print(f"  WARNING: live stickers {len(cids)} != plan {len(plan)} and no "
                  f"upload-order record; mapping by position may be WRONG. "
                  f"Run coins/remap_ids.py to fix by image content.", flush=True)
        for i, cid in enumerate(cids):
            if i >= len(plan):
                break
            for t in plan[i]["tickers"]:
                ticker_to_id[t] = cid
    reapply_aliases(ticker_to_id)
    TICKER_IDS.write_text(json.dumps(ticker_to_id, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"mapped {len(ticker_to_id)} tickers across {len(sets)} sets "
          f"({len(cids)} live stickers)", flush=True)
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


def send_final_links(tg: Telegram) -> None:
    state = load_state()
    sets = sorted(state["sets"], key=lambda x: x["index"])
    if not sets:
        print("no sets to send.", flush=True)
        return
    lines = [f"{s['index']}. https://t.me/addemoji/{s['name']}" for s in sets]
    text = "\U0001F4E6 @YourBrand Crypto Emoji \u2014 all packs:\n" + "\n".join(lines)
    msg(tg, text)
    state["final_sent"] = True
    save_state(state)
    print(f"sent final combined message with {len(sets)} links (preview off).", flush=True)


if __name__ == "__main__":
    load_env()
    _tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    _bot = _tg.get_me()["username"]
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "map":
        map_and_fill(_tg)
    elif arg == "links":
        send_final_links(_tg)
    elif arg == "build":
        # Build only. Exit 0 when ALL plan images are live, else exit 3 so an
        # external restart loop can resume (build is resumable + duplicate-proof).
        import traceback
        plan = load_plan()
        try:
            build(_tg, _bot)
        except Exception:  # noqa: BLE001 - log full cause, let the loop resume
            traceback.print_exc()
        state = load_state()
        live = sum(live_count(_tg, s["name"]) for s in state["sets"])
        print(f"buildonly checkpoint: {live}/{len(plan)} live", flush=True)
        raise SystemExit(0 if live >= len(plan) else 3)
    else:
        build(_tg, _bot)
        map_and_fill(_tg)
        send_final_links(_tg)
