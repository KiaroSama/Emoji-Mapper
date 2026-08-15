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
    present), so interrupting and resuming can never create a duplicate.
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

import argparse
import csv
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image

from build_pack import (AmbiguousUploadError, Telegram, load_env,
                        write_json_atomic)

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
# Above this, a set of tickers sharing one emoji id is treated as corruption
# rather than a shared logo. Real shared-logo groups are one asset on several
# chains (USDT on 8, USDC on 9); the positional-drift bug produced a group of
# 129 unrelated coins.
SHARED_GROUP_LIMIT = 20
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
    return sum(1 for v in alpha.get_flattened_data() if v > 10) <= min_visible


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
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Never fall back to the empty default: that resets deleted_old and
            # the cursor, so the whole plan is uploaded again as duplicates.
            raise SystemExit(
                f"ERROR: resume state {STATE.name} is unreadable ({exc}).\n"
                f"       Refusing to restart from zero -- that would re-upload "
                f"every image already published.\n"
                f"       Inspect the file (a .tmp sibling may hold the last "
                f"write) and restore it deliberately.")
    return {"sets": [], "sent": [], "deleted_old": False, "final_sent": False,
            "order": [], "cursor": 0, "in_flight": None}


def save_state(s: dict) -> None:
    write_json_atomic(STATE, s)


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

    if not USER_ID:
        raise SystemExit("ERROR: PACK_OWNER_USER_ID is not set (env or .env); "
                         "refusing to start a rebuild without a pack owner.")
    # Validate BEFORE destroying anything. An empty or unusable plan (missing
    # emoji directory, unreadable plan file) would otherwise delete every
    # existing pack and then have nothing to rebuild them from.
    if not plan:
        raise SystemExit(
            f"ERROR: the rebuild plan is empty ({PLAN.name}); nothing to build.\n"
            f"       Expected prepared 100x100 PNGs in {EMOJI}.\n"
            f"       Refusing to delete the existing packs.")

    if not state.get("deleted_old"):
        print(f"deleting ALL old packs and rebuilding {len(plan)} images...",
              flush=True)
        delete_old_packs(tg)
        state["deleted_old"] = True
        save_state(state)

    # Resume position comes from the RECORDED cursor, never from the live
    # sticker count. A plan entry that is skipped (missing / blank / failed)
    # consumes a plan position but produces no sticker, so `sum(live)` drifts
    # behind the plan index by one per skip -- resuming at plan[sum(live)] then
    # re-uploads entries that are already published, which is exactly how
    # duplicates and the mis-aligned ticker map were produced.
    state.setdefault("cursor", 0)
    state.setdefault("in_flight", None)
    cum = 0
    for s in state["sets"]:
        s["live"] = live_count(tg, s["name"])
        cum += s["live"]

    # An upload recorded as in flight may or may not have landed; the live count
    # answers that exactly, for that one entry.
    if state["in_flight"]:
        if cum == len(state["order"]) + 1:
            state["order"].append(state["in_flight"])
            print(f"  resume: {state['in_flight']} did land before the "
                  f"interruption", flush=True)
        else:
            print(f"  resume: {state['in_flight']} did not land; retrying",
                  flush=True)
            state["cursor"] = max(0, state["cursor"] - 1)
        state["in_flight"] = None
        save_state(state)

    if cum != len(state["order"]):
        raise SystemExit(
            f"ERROR: {cum} stickers are live but only {len(state['order'])} "
            f"uploads are recorded.\n"
            f"       Refusing to continue: the recorded order is what maps "
            f"stickers to tickers, and continuing from a disagreeing state is "
            f"what corrupts ticker_to_id.json.\n"
            f"       Reconcile with coins/remap_ids.py, or delete the packs and "
            f"the state file to rebuild cleanly.")

    print(f"resume: {len(state['sets'])} sets, {cum} live stickers, "
          f"plan position {state['cursor']}/{len(plan)}", flush=True)

    if state["sets"] and state["sets"][-1]["live"] < PER_SET:
        cur = state["sets"][-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index, set_name, in_set = len(state["sets"]), "", 0

    for plan_i in range(state["cursor"], len(plan)):
        g = plan[plan_i]
        png = EMOJI / f"{g['rep']}.png"
        # Permanent skips: deterministic, so simply advancing past them is safe.
        if not png.is_file() or png.stat().st_size == 0:
            print(f"  skip {g['rep']}: missing/empty", flush=True)
            state["cursor"] = plan_i + 1
            continue
        if is_blank(png):
            print(f"  skip {g['rep']}: blank image (no blank emoji)", flush=True)
            state["cursor"] = plan_i + 1
            continue
        kw = g["kw"]
        # Write the intent before the request, so an interruption anywhere in
        # the upload leaves an exact record of which entry was in flight.
        state["cursor"] = plan_i + 1
        state["in_flight"] = g["rep"]
        save_state(state)
        try:
            placed = False
            if in_set != 0:
                try:
                    tg.add_sticker(USER_ID, set_name, png, EMOJI_CHAR, kw,
                                   expected_before=in_set)
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
        except AmbiguousUploadError as exc:
            # Never blind-retry a maybe-applied call (that duplicates emoji in
            # the pack). Adopt a create that verifiably landed; anything else
            # is healed by the live-count reconcile on the next run.
            if not placed and in_set == 0:
                known, sset = tg.probe_sticker_set(set_name)
                if known and sset is not None and len(sset.get("stickers", [])) == 1:
                    state["sets"].append({"index": set_index, "name": set_name,
                                          "title": title})
                    save_state(state)
                    print(f"[set {set_index}] adopted {set_name} after ambiguous "
                          f"create", flush=True)
                else:
                    set_index -= 1
                    print(f"  {g['rep']}: {exc}; reconciled on next run", flush=True)
                    save_state(state)   # keep in_flight: next run resolves it
                    continue
            else:
                print(f"  {g['rep']}: {exc}; reconciled on next run", flush=True)
                save_state(state)       # keep in_flight: next run resolves it
                continue
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            print(f"  skip {g['rep']}: {exc}", flush=True)
            state["in_flight"] = None   # definitively not applied
            save_state(state)
            continue
        in_set += 1
        state["order"].append(g["rep"])  # record actual upload order
        state["in_flight"] = None
        save_state(state)                # persist before the next request
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


def approved_shared_tickers() -> set[str]:
    """Tickers reviewed as legitimately sharing a logo (shared_logo_groups.json)."""
    if not GROUPS_REPORT.is_file():
        return set()
    try:
        groups = json.loads(GROUPS_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {t for members in groups.values() for t in members}


def unapproved_shared_groups(mapping: dict[str, str]) -> dict[str, list[str]]:
    """Emoji ids claimed by several tickers that were never reviewed as shared.

    Two coins may legitimately share one image (Tether on six chains), and those
    groups are recorded in shared_logo_groups.json. Anything else pointing many
    tickers at one id is the signature of a mis-aligned mapping, not a shared
    logo -- and it is silent, because the map stays structurally valid.
    """
    approved = approved_shared_tickers()
    by_id: dict[str, list[str]] = defaultdict(list)
    for ticker, cid in mapping.items():
        by_id[str(cid)].append(ticker)
    return {cid: tickers for cid, tickers in by_id.items()
            if len(tickers) > 1 and not set(tickers) <= approved}


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
    # The ONLY sound mapping is the recorded upload order. The former positional
    # fallback ("assume live order == plan order") printed a warning and then
    # overwrote the canonical map anyway; with skipped entries the assignments
    # drift by one per skip, which is what put 129 unrelated tickers on a single
    # emoji id. There is no safe guess here -- refuse instead.
    if len(order) != len(cids) or not order:
        candidate = ROOT / "ticker_to_id.candidate.json"
        write_json_atomic(candidate, {
            "error": "upload-order record does not match live stickers",
            "recorded_uploads": len(order), "live_stickers": len(cids),
            "plan_entries": len(plan),
        })
        raise SystemExit(
            f"ERROR: {len(order)} recorded uploads but {len(cids)} live "
            f"stickers.\n"
            f"       Refusing to write {TICKER_IDS.name} from positional "
            f"guesswork -- that is what corrupts the map.\n"
            f"       Rebuild identities from image content with "
            f"coins/remap_ids.py. Details: {candidate.name}")

    by_rep = {g["rep"]: g for g in plan}
    for i, cid in enumerate(cids):
        g = by_rep.get(order[i])
        if not g:
            continue
        for t in g["tickers"]:
            ticker_to_id[t] = cid
    print(f"mapped via recorded upload order ({len(cids)} stickers)", flush=True)

    # The same plan entry appearing twice means the image really was uploaded
    # twice -- a duplicate in the pack, and the map would silently keep only the
    # later id.
    repeated = sorted({rep for rep in order if order.count(rep) > 1})
    if repeated:
        raise SystemExit(
            f"ERROR: {len(repeated)} image(s) were uploaded more than once "
            f"(e.g. {repeated[:5]}).\n"
            f"       Refusing to write {TICKER_IDS.name} over a pack that "
            f"contains duplicates. Remove the extra stickers first.")

    reapply_aliases(ticker_to_id)

    bad = unapproved_shared_groups(ticker_to_id)
    oversized = {cid: ts for cid, ts in bad.items() if len(ts) > SHARED_GROUP_LIMIT}
    if oversized:
        biggest = max(oversized.values(), key=len)
        candidate = ROOT / "ticker_to_id.candidate.json"
        write_json_atomic(candidate, ticker_to_id)
        raise SystemExit(
            f"ERROR: one emoji id would be shared by {len(biggest)} unreviewed "
            f"tickers (e.g. {sorted(biggest)[:6]}).\n"
            f"       A group that large is the signature of a mis-aligned "
            f"mapping, not a shared logo. Refusing to overwrite "
            f"{TICKER_IDS.name}; review {candidate.name} instead.")
    if bad:
        print(f"  note: {len(bad)} shared-logo group(s) are not listed in "
              f"{GROUPS_REPORT.name} (largest {max(len(t) for t in bad.values())}). "
              f"Cross-chain variants of one asset are expected here; add them to "
              f"that file to silence this.", flush=True)

    write_json_atomic(TICKER_IDS, ticker_to_id)
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
    # Explicit parsing: the previous code treated ANY unrecognised first
    # argument -- including a typo like "buid" -- as "run the full destructive
    # rebuild", so a slip deleted every pack. Unknown input must fail closed.
    _ap = argparse.ArgumentParser(
        description="Deduplicated rebuild of the crypto custom-emoji packs.")
    _ap.add_argument("command", nargs="?", default="all",
                     choices=["all", "build", "map", "links"],
                     help="all = delete old packs + build + map + links "
                          "(DESTRUCTIVE); build = upload only; "
                          "map = rebuild ticker_to_id.json; links = resend links")
    _args = _ap.parse_args()

    load_env()
    _tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    _bot = _tg.get_me()["username"]
    arg = _args.command
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
        cursor = state.get("cursor", 0)
        print(f"buildonly checkpoint: plan position {cursor}/{len(plan)}, "
              f"{live} live stickers", flush=True)
        # Done means "walked the whole plan", NOT "live count reached the plan
        # length". Entries that are permanently skipped (missing/blank image)
        # never become stickers, so a live-count gate can never be satisfied and
        # the restart loop keeps re-running forever, adding duplicates.
        raise SystemExit(0 if cursor >= len(plan) else 3)
    else:
        build(_tg, _bot)
        map_and_fill(_tg)
        send_final_links(_tg)
