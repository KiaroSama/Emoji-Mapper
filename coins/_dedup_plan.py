"""What the deduplicated rebuild intends to upload, and how far it got.

The frozen plan and the resume state, plus the constants both phases and
the CLI share. Bottom layer: it imports neither the build phase nor the
map phase.
"""

from __future__ import annotations

# This module lives in coins/; allow importing the shared engine from the
# project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from PIL import Image

from build_pack import EXIT_PARTIAL, load_env, safe_int_env
from emojikit import media
from packstate import (pack_family_lock_path, write_json_atomic)


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

BASE = "gvcryptoemoji"
# One lock for the whole pack family, keyed on BASE. Naming it after this
# tool's state file made it a different lock from the fetchers' coin_pack.lock,
# so a rebuild and a provider top-up could append to the same live sets at once.
#
# LOCK ORDER, project-wide: the pack-family lock is always taken BEFORE
# canonical_map_lock(), never the other way round (rebuild_dedup.build,
# rebuild_dedup.map_and_fill, verify_logos --fix, fetch_paprika.publish_logos
# all follow it). A tool that only rewrites the map -- alias_map, enhance_map,
# remap_ids --apply -- takes the map lock alone, so no cycle exists.
LOCK = pack_family_lock_path(BASE)
TITLE = "@GodVerify Crypto Emoji"
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


def is_blank(path: Path) -> bool:
    """True if an image is effectively empty -- never upload a blank emoji.

    Delegates: this carried its own per-pixel loop and its own thresholds, so
    the rule that decides what ships had a definition here AND in
    emojikit.media -- and this one was the slow form media had already moved
    away from.
    """
    try:
        return media.is_blank_image(Image.open(path))
    except Exception:  # noqa: BLE001 - unreadable is not publishable
        return True


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


def _count(value) -> bool:
    """A plain non-negative integer. ``bool`` is an ``int`` in Python, and
    ``True`` sailing through as the number 1 is how a corrupt field becomes a
    plausible-looking count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _order_walks_plan(order: list[str], processed: list[str]) -> str:
    """Why ``order`` cannot be the uploads of ``processed``, or "".

    ``order`` must be a SUBSEQUENCE of the plan prefix the cursor claims to
    have walked. It is not the same list: an entry can be skipped (missing,
    blank, permanently rejected by Telegram) and produce no sticker. But
    nothing can be uploaded that the cursor never reached, nothing twice, and
    nothing out of plan order -- each of those means the cursor and the upload
    record describe different walks, and the build would either re-upload a
    live image or skip one it believes is done.
    """
    remaining = iter(processed)
    for rep in order:
        # ``in`` on an ITERATOR consumes up to and including the match, so this
        # loop is exactly the subsequence test, in one pass.
        if rep not in remaining:
            return (f"'order' records {rep!r}, which is not among the first "
                    f"{len(processed)} plan entries the cursor claims were "
                    f"processed, in that order")
    return ""


def _state_problem(s, plan: list[dict] | None) -> str:
    """Why this resume state must not drive a mutation, or "" when it may.

    build_pack.validate_state_shape covers the *publisher* schema (base/done/
    count); the rebuild keeps a different one (order/cursor/live), so its
    invariants are checked here. Valid JSON is not valid state: cursor=-1 makes
    ``plan[-1]`` the first upload AND leaves it to be uploaded again at the end,
    and a cursor past the plan reports the rebuild finished without ever having
    walked it.

    ``plan`` is the frozen upload order. With it, cursor and order are checked
    against each other rather than only for shape -- cursor=0 beside
    order=["aaa"] is structurally perfect and still means the build is about to
    upload ``aaa`` a second time.
    """
    if not isinstance(s, dict):
        return "state is not an object"
    for field in ("sets", "sent", "order", "deleted_old_packs",
                  "provider_added"):
        if not isinstance(s.get(field, []), list):
            return f"{field!r} must be a list"
    for field in ("deleted_old", "final_sent"):
        if not isinstance(s.get(field, False), bool):
            return f"{field!r} must be true or false"
    for field in ("sent", "order", "deleted_old_packs", "provider_added"):
        if not all(isinstance(x, str) and x for x in s.get(field, [])):
            return f"{field!r} must hold non-empty names"

    topped = s.get("provider_added", [])
    dup_top = sorted({t for t in topped if topped.count(t) > 1})
    if dup_top:
        # The tally is counted against live stickers, so a repeat inflates
        # the total and hides exactly the drift the count exists to catch.
        return f"'provider_added' repeats {dup_top}"

    order = s.get("order", [])
    repeated = sorted({rep for rep in order if order.count(rep) > 1})
    if repeated:
        # One plan entry recorded twice means the image really is in the pack
        # twice; mapping would silently keep only the later id.
        return f"'order' records {repeated[:5]} more than once"

    seen_names: set[str] = set()
    last_index = 0
    for i, entry in enumerate(s.get("sets", [])):
        if not isinstance(entry, dict):
            return f"sets[{i}] is not an object"
        name, index = entry.get("name"), entry.get("index")
        if not isinstance(name, str) or not name:
            return f"sets[{i}] has no name"
        if name in seen_names:
            return f"sets[{i}] repeats the set name {name!r}"
        if not _count(index) or index < 1:
            return f"sets[{i}] has a bad index {index!r}"
        if index <= last_index:
            return f"sets[{i}] index {index} does not ascend"
        if not _count(entry.get("live", 0)) or entry.get("live", 0) > PER_SET:
            return f"sets[{i}] live count {entry.get('live')!r} outside 0..{PER_SET}"
        seen_names.add(name)
        last_index = index

    cursor = s.get("cursor", 0)
    if not _count(cursor):
        return f"cursor {cursor!r} is not a plan position"
    reps = None if plan is None else [g.get("rep") for g in plan]
    if reps is not None:
        if cursor > len(reps):
            return (f"cursor {cursor} is past the end of the {len(reps)}-entry "
                    f"plan")
        walk = _order_walks_plan(order, reps[:cursor])
        if walk:
            return walk
    if order and not s.get("sets", []):
        # Every recorded upload went into a recorded set. Uploads with nowhere
        # to live means the two halves of the state came from different runs.
        return f"{len(order)} upload(s) are recorded but no set holds them"

    marker = s.get("in_flight")
    if marker is None:
        return ""
    if isinstance(marker, str):
        # The legacy bare-key marker, from before the intent carried a set
        # name. Accepting it here only moved the failure to
        # _reconcile_in_flight, which then has no set to probe and stops with
        # EXIT_PARTIAL -- every run stopping at the same point, and nothing
        # able to clear it. Resolving the set instead is not available: 5d4669b
        # wrote this marker BEFORE choosing between add and create, so it can
        # name a create whose set never reached state["sets"], and probing the
        # last recorded set would judge that create "did not land" and upload
        # the image a second time.
        # The repair has to name BOTH halves of the bookkeeping. The cursor is
        # advanced past an entry before its mutation is recorded, and 'order'
        # only gains the key on success -- so clearing the marker alone leaves
        # one more live sticker than recorded uploads, and the consistency gate
        # in build() then stops the run again, offering only remap_ids or a full
        # destructive rebuild. Half a repair is a second permanent stop.
        return (f"in_flight is the legacy bare key {marker!r}, which names no "
                f"set to probe. Check by hand whether {marker}.png is live in "
                f"the newest pack, then repair the state: if it IS live, set "
                f"'in_flight' to null AND append {marker!r} to 'order'; if it "
                f"is NOT live, set 'in_flight' to null AND step 'cursor' back "
                f"by one so it is retried")
    if not isinstance(marker, dict):
        return "'in_flight' must be an intent object or null"
    for field in ("key", "operation", "set_name"):
        if not isinstance(marker.get(field), str) or not marker[field]:
            return f"in_flight is missing {field!r}"
    if marker["operation"] not in ("add", "create"):
        return f"in_flight operation {marker['operation']!r} is unknown"
    if not _count(marker.get("set_index")):
        return f"in_flight set_index {marker.get('set_index')!r} is not a set number"
    expected = marker.get("expected_before")
    if expected is not None and not _count(expected):
        return f"in_flight expected_before {expected!r} is not a count"
    if reps is not None and (not cursor or reps[cursor - 1] != marker["key"]):
        # The cursor is advanced past an entry BEFORE its mutation is recorded,
        # so an in-flight marker is always the entry the cursor just passed.
        # Any other pairing reconciles one plan entry's outcome onto another.
        return (f"in_flight {marker['key']!r} is not plan entry {cursor}, the "
                f"one the cursor was moved past to run it")
    return ""


def load_state(plan: list[dict] | None = None) -> dict:
    """Load the resume state, refusing anything a mutation cannot be built on.

    Callers that are about to upload or delete pass the frozen ``plan`` so the
    cursor, the recorded upload order and the in-flight marker are checked
    against the list they all index -- not merely against each other.
    """
    if not STATE.is_file():
        return {"sets": [], "sent": [], "deleted_old": False, "final_sent": False,
                "order": [], "cursor": 0, "in_flight": None}
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Never fall back to the empty default: that resets deleted_old and
        # the cursor, so the whole plan is uploaded again as duplicates.
        raise SystemExit(
            f"ERROR: resume state {STATE.name} is unreadable ({exc}).\n"
            f"       Refusing to restart from zero -- that would re-upload "
            f"every image already published.\n"
            f"       Inspect the file (a .tmp sibling may hold the last "
            f"write) and restore it deliberately.") from exc
    problem = _state_problem(state, plan)
    if problem:
        raise SystemExit(
            f"ERROR: resume state {STATE.name} is not internally consistent "
            f"({problem}).\n"
            f"       Refusing to delete or upload anything from it: every "
            f"resume decision -- which set is active, which plan entry is next "
            f"-- is built on these numbers.\n"
            f"       Inspect the file (a .tmp sibling may hold the last "
            f"write) and repair it deliberately.")
    return state


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
