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
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image

from build_pack import (EXIT_OK, EXIT_PARTIAL, AmbiguousUploadError,
                        LiveStateUnknown, SetState, Telegram, exclusive_lock,
                        links_chat_id, load_env, pack_family_lock_path,
                        safe_int_env, write_json_atomic)
from emojikit.media import _dhash, hamming

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
# One lock for the whole pack family, keyed on BASE. Naming it after this
# tool's state file made it a different lock from the fetchers' coin_pack.lock,
# so a rebuild and a provider top-up could append to the same live sets at once.
LOCK = pack_family_lock_path(BASE)
TITLE = "@YourBrand Crypto Emoji"
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
# Above this, a set of tickers sharing one emoji id is treated as corruption
# rather than a shared logo. Real shared-logo groups are one asset on several
# chains (USDT on 8, USDC on 9); the positional-drift bug produced a group of
# 129 unrelated coins.
SHARED_GROUP_LIMIT = 20
# A live sticker within this perceptual distance of the PNG we sent IS that
# upload: Telegram re-encodes PNG to WEBP, so identical content still differs by
# a bit or two. Same budget as the fetchers.
SAME_IMAGE_MAX = 8
load_env()  # ensure .env is loaded before resolving the owner id at import
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
# safe_int_env, not int(): a typo in .env must not raise at import, before
# argparse can explain what is wrong.
USER_ID = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)


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

    # Atomic: an interrupted write must not leave a truncated plan behind, because
    # the plan is the frozen upload order and load_plan() would read the surviving
    # prefix as the whole plan.
    write_json_atomic(PLAN, groups)
    # Documentation: only the shared-logo groups (>1 coin per image).
    shared = {g["rep"]: g["tickers"] for g in groups if len(g["tickers"]) > 1}
    write_json_atomic(GROUPS_REPORT, shared)
    dup_extra = sum(len(g["tickers"]) - 1 for g in groups)
    print(f"plan: {len(groups)} unique images | shared-logo groups: {len(shared)} "
          f"| coins collapsing onto a shared image: {dup_extra}", flush=True)
    return groups


def _plan_is_sound(plan) -> bool:
    """Every entry must carry the fields build()/map_and_fill() index it by."""
    return bool(plan) and isinstance(plan, list) and all(
        isinstance(g, dict) and isinstance(g.get("rep"), str) and g["rep"]
        and isinstance(g.get("tickers"), list) and g["tickers"]
        and isinstance(g.get("kw"), str)
        for g in plan)


def load_plan() -> list[dict]:
    """Load the frozen plan; build it only when there is none.

    A truncated or malformed plan must fail closed. It is the canonical upload
    order, so silently regenerating it -- or accepting the surviving prefix of a
    half-written one -- renumbers entries that are already live and re-uploads
    them as duplicates.
    """
    if not PLAN.is_file():
        return build_plan()
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        plan, why = None, str(exc)
    else:
        why = "" if _plan_is_sound(plan) else "entries are missing rep/tickers/kw"
    if why:
        raise SystemExit(
            f"ERROR: the rebuild plan {PLAN.name} is unusable ({why}).\n"
            f"       Refusing to rebuild it: the plan is the frozen upload "
            f"order, and a fresh one would not line up with what is already "
            f"live.\n"
            f"       Restore it (a .tmp sibling may hold the last write), or "
            f"delete the packs and the state file to rebuild cleanly.")
    return plan


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


def _stop_retryable(reason: str) -> None:
    """End the run before the next mutation, with the "resume me" exit code.

    Anything unresolved -- an ambiguous upload, a live state that could not be
    read -- must stop the process here. Continuing would overwrite the in-flight
    marker that is the only record of what is unresolved, and would mutate packs
    whose real contents are unknown.
    """
    print(f"STOP: {reason}", flush=True)
    raise SystemExit(EXIT_PARTIAL)


# Deterministic rejections of THIS image: the same bytes will be refused again,
# so the plan entry can be skipped for good. Everything else -- a transport
# failure, a 5xx, a vanished set, a name collision -- is retryable and must NOT
# consume the plan position.
PERMANENT_MEDIA_ERRORS = (
    "STICKER_PNG_NOPNG", "STICKER_PNG_DIMENSIONS", "STICKER_DIMENSIONS_INVALID",
    "STICKER_FILE_INVALID", "STICKER_TOO_BIG", "STICKER_EMOJI_INVALID",
    "INVALID_STICKER_EMOJIS", "IMAGE_PROCESS_FAILED", "PHOTO_INVALID_DIMENSIONS",
    "STICKER_TGS_NOTGS", "STICKER_VIDEO_NOWEBM", "FILE MUST BE NON-EMPTY",
)


def _is_permanent_media_error(exc: BaseException) -> bool:
    """True when Telegram rejected the IMAGE, not the attempt."""
    text = str(exc).upper()
    return any(marker in text for marker in PERMANENT_MEDIA_ERRORS)


def _mark_in_flight(state: dict, key: str, operation: str, set_name: str,
                    set_index: int, expected_before: int) -> None:
    """Record WHICH mutation is about to run, before running it.

    Structured, not a bare ticker: a restart has to reconcile an ambiguous
    create whose set never reached state["sets"], and that needs the set name
    and index as well as the plan key.
    """
    state["in_flight"] = {
        "key": key, "operation": operation, "set_name": set_name,
        "set_index": set_index, "expected_before": expected_before,
        "phase": "upload",
    }
    save_state(state)


def _as_marker(marker) -> dict:
    """Accept the bare-'rep' in-flight marker written by older runs."""
    if isinstance(marker, str):
        return {"key": marker, "operation": "add", "set_name": "",
                "set_index": 0, "expected_before": None, "phase": "upload"}
    return marker


def msg(tg: Telegram, text: str) -> None:
    """Announce pack links, with link previews disabled.

    Goes to PACK_LINKS_CHAT_ID when configured (a channel the bot administers),
    otherwise the owner's private chat.

    ``retries=2`` on purpose, matching Telegram.send_message: sendMessage is not
    idempotent and has no dedup key, so a timeout AFTER Telegram accepted the
    post cannot be told from one before it -- the default five attempts turn a
    single outage into five identical link messages.
    """
    tg._call("sendMessage", retries=2, data={
        "chat_id": links_chat_id(USER_ID), "text": text,
        "disable_web_page_preview": True,
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


def delete_old_packs(tg: Telegram, state: dict) -> bool:
    """Delete the old packs; True only when every one is confirmed gone.

    A printed delete failure used to be enough for build() to record the phase
    as complete, so a pack that survived was never retried and the rebuild
    published a second family beside it. Deletion status is now per pack and
    persisted, and only a live probe closes one out.
    """
    if not OLD_STATE.is_file():
        return True
    old = json.loads(OLD_STATE.read_text(encoding="utf-8"))
    names = [s["name"] for s in old.get("sets", [])]
    gone = set(state.setdefault("deleted_old_packs", []))
    for name in names:
        if name in gone:
            continue
        try:
            tg._call("deleteStickerSet", data={"name": name})
        except Exception as exc:  # noqa: BLE001 - the probe below is the verdict
            print(f"  (old pack {name}: {exc})", flush=True)
        # Only live state proves a delete: the call can fail after applying it,
        # and can succeed for a set that was already gone.
        set_state, _ = tg.probe_set_state(name)
        if set_state is SetState.UNKNOWN:
            _stop_retryable(f"cannot confirm old pack {name} was deleted")
        if set_state is SetState.MISSING:
            gone.add(name)
            state["deleted_old_packs"] = sorted(gone)
            save_state(state)
            print(f"  deleted old pack {name}", flush=True)
        else:
            print(f"  old pack {name} still exists after delete", flush=True)
        time.sleep(0.5)
    return set(names) <= gone


def _marked_add_landed(tg: Telegram, marker: dict) -> bool:
    """Did the in-flight ADD land, judged by IMAGE identity?

    A count cannot answer this. "The set is one longer than expected_before"
    is equally true when someone added a sticker by hand or a concurrent tool
    appended one -- and the resume then records OUR ticker as uploaded, so the
    image is never sent and the map points the ticker at a stranger's sticker.
    Stops the run whenever live state does not answer.
    """
    png = EMOJI / f"{marker['key']}.png"
    if not marker.get("set_name") or not png.is_file():
        _stop_retryable(f"cannot check the in-flight {marker['key']}: its set "
                        f"name or source image is gone")
    set_state, sset = tg.probe_set_state(marker["set_name"])
    if set_state is SetState.UNKNOWN:
        _stop_retryable(f"live state of {marker['set_name']} is unknown; cannot "
                        f"tell whether the in-flight {marker['key']} landed")
    if set_state is SetState.MISSING:
        return False
    before = marker.get("expected_before") or 0
    want = _dhash(Image.open(png).convert("RGBA"))
    hits = 0
    with tempfile.TemporaryDirectory() as tmp:
        for st in (sset.get("stickers") or [])[before:]:
            dest = Path(tmp) / str(st.get("file_unique_id") or st.get("file_id"))
            try:
                tg.download_file(str(st["file_id"]), dest)
                got = _dhash(Image.open(dest).convert("RGBA"))
            except Exception as exc:  # noqa: BLE001
                # Unreadable is not "not ours": guessing here re-uploads an
                # image that is already live.
                _stop_retryable(f"sticker {st.get('custom_emoji_id')} in "
                                f"{marker['set_name']} could not be read ({exc})")
            if hamming(want, got) <= SAME_IMAGE_MAX:
                hits += 1
    if hits > 1:
        _stop_retryable(f"{hits} live stickers carry {png.name}; the pack "
                        f"already contains a duplicate")
    return hits == 1


def _reconcile_in_flight(tg: Telegram, state: dict, cum: int) -> int:
    """Resolve the recorded in-flight mutation against live state.

    Returns the live sticker total, which grows if an ambiguous create is found
    to have landed. That case is invisible to the per-set count in build():
    the set was never recorded in state["sets"], so its stickers are counted
    nowhere and the entry would be blamed as "did not land" and re-uploaded.
    """
    marker = _as_marker(state["in_flight"])
    known_sets = {s["name"] for s in state["sets"]}
    landed = False
    if marker.get("operation") == "create":
        # A create is self-identifying: the set carries our sticker because the
        # call that made the set is the call that put it there.
        set_state, sset = tg.probe_set_state(marker["set_name"])
        if set_state is SetState.UNKNOWN:
            _stop_retryable(f"live state of {marker['set_name']} is unknown; "
                            f"cannot tell whether the in-flight create landed")
        landed = set_state is SetState.EXISTS
        if landed and marker["set_name"] not in known_sets:
            live = len(sset.get("stickers", []))
            index = marker.get("set_index") or len(state["sets"]) + 1
            state["sets"].append({"index": index, "name": marker["set_name"],
                                  "title": f"{TITLE} {index}", "live": live})
            cum += live
            print(f"  resume: adopted {marker['set_name']}, created before the "
                  f"interruption", flush=True)
    else:
        landed = _marked_add_landed(tg, marker)

    if landed:
        state["order"].append(marker["key"])
        print(f"  resume: {marker['key']} did land before the interruption",
              flush=True)
    else:
        print(f"  resume: {marker['key']} did not land; retrying", flush=True)
        state["cursor"] = max(0, state["cursor"] - 1)
    state["in_flight"] = None
    save_state(state)
    return cum


def build(tg: Telegram, bot: str) -> None:
    """Upload the plan, one exclusive run at a time.

    Two concurrent runs sharing this state read the same cursor, upload the
    same plan entries and duplicate them in the pack -- and a pack has no
    unique constraint that would catch it afterwards.
    """
    with exclusive_lock(LOCK):
        _build(tg, bot)


def _build(tg: Telegram, bot: str) -> None:
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
        if not delete_old_packs(tg, state):
            _stop_retryable("some old packs still exist; refusing to build a "
                            "second family beside them")
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
    try:
        for s in state["sets"]:
            s["live"] = tg.live_count_strict(s["name"])
            cum += s["live"]
    except LiveStateUnknown as exc:
        # A live read that failed is not "the set is empty": treating it as 0
        # moves the cursor backwards and re-sends an upload that already landed.
        _stop_retryable(f"{exc}; refusing to resume from a guessed live count")

    # An upload recorded as in flight may or may not have landed; live state
    # answers that exactly, for that one entry.
    if state["in_flight"]:
        cum = _reconcile_in_flight(tg, state, cum)

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
        state["cursor"] = plan_i + 1
        try:
            placed = False
            if in_set != 0:
                # Write the intent before the request, so an interruption
                # anywhere in the upload leaves an exact record of which
                # mutation was in flight.
                _mark_in_flight(state, g["rep"], "add", set_name, set_index, in_set)
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
                _mark_in_flight(state, g["rep"], "create", set_name, set_index, 0)
                tg.create_set(USER_ID, set_name, title, png, EMOJI_CHAR, kw)
                state["sets"].append({"index": set_index, "name": set_name,
                                      "title": title})
                save_state(state)
                print(f"[set {set_index}] created {set_name}", flush=True)
        except AmbiguousUploadError as exc:
            # The call may or may not have been applied. Never blind-retry it
            # (that duplicates the emoji) and never run another mutation: the
            # next one would overwrite the marker that identifies this one.
            # The marker is already on disk; the reconcile at the start of the
            # next run resolves it against live state.
            _stop_retryable(f"{g['rep']}: {exc}; resolved on the next run")
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            # _call raises RuntimeError only once the change is verified NOT
            # applied, so the marker is resolved either way.
            state["in_flight"] = None
            if _is_permanent_media_error(exc):
                # Deterministic rejection: the same bytes will be refused
                # again, so the entry is skipped and the cursor stays past it.
                print(f"  skip {g['rep']}: {exc}", flush=True)
                save_state(state)
                continue
            # Retryable transport/API failure. The cursor was moved past this
            # entry BEFORE the request, so it must go back onto it -- and the
            # run has to stop here: the loop range was fixed before the first
            # iteration, so continuing would upload later entries that the
            # rolled-back cursor would then upload AGAIN on the next run.
            state["cursor"] = plan_i
            save_state(state)
            _stop_retryable(f"{g['rep']}: {exc}; not applied, retried next run")
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
    # Same lock as build(): this is a read-modify-write of the rebuild state, so
    # running it beside a build would save a stale snapshot back over the
    # upload order recorded in the meantime.
    with exclusive_lock(LOCK):
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
        print(f"sent final combined message with {len(sets)} links (preview off).",
              flush=True)


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
        # A stop inside build() raises SystemExit with its own retryable code and
        # skips this checkpoint on purpose.
        cursor = load_state().get("cursor", 0)
        print(f"buildonly checkpoint: plan position {cursor}/{len(plan)}",
              flush=True)
        # Done means "walked the whole plan", NOT "live count reached the plan
        # length". Entries that are permanently skipped (missing/blank image)
        # never become stickers, so a live-count gate can never be satisfied and
        # the restart loop keeps re-running forever, adding duplicates.
        raise SystemExit(EXIT_OK if cursor >= len(plan) else EXIT_PARTIAL)
    else:
        build(_tg, _bot)
        map_and_fill(_tg)
        send_final_links(_tg)
