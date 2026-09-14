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
  - New pack base name 'gvce' (the old 'gvcryptoemoji' names are being deleted;
    a fresh base avoids name-reuse conflicts). Titles: '@GodVerify Crypto Emoji N'.

Usage:
  python rebuild_dedup.py            # build plan (if needed) + delete old + build + map
  python rebuild_dedup.py map        # skip building; map live cids + fill inventory
  python rebuild_dedup.py links      # (re)send the final combined links message
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from PIL import Image

from emojikit.build_pack import (EXIT_OK, EXIT_PARTIAL, load_env)
from emojikit.announce import (announce_packs)
from emojikit.packstate import (exclusive_lock)
from emojikit.telegram_api import (AmbiguousUploadError, LiveStateUnknown, SetState, Telegram)
from emojikit.identity import _dhash, hamming

from coins._dedup_map import map_and_fill, send_final_links
from coins import _dedup_plan as cfg
from coins._dedup_plan import (_stop_retryable, is_blank, load_plan, load_state, save_state)

def _is_permanent_media_error(exc: BaseException) -> bool:
    """True when Telegram rejected the IMAGE, not the attempt."""
    text = str(exc).upper()
    return any(marker in text for marker in cfg.PERMANENT_MEDIA_ERRORS)


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


def notify(tg: Telegram, state: dict, name: str, title: str) -> None:
    if name in state["sent"]:
        return
    try:
        # Shared with the other two publishers so the coin rebuild cannot be
        # the one left talking to Telegram directly after a Worker is deployed.
        dest = announce_packs(tg, cfg.USER_ID, [{"name": name, "title": title}],
                              bot="coin")
        state["sent"].append(name)
        save_state(state)
        print(f"  sent link for {name} to {dest}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  notify failed {name}: {exc}", flush=True)


def delete_old_packs(tg: Telegram, state: dict) -> bool:
    """Delete the old packs; True only when every one is confirmed gone.

    A printed delete failure used to be enough for build() to record the phase
    as complete, so a pack that survived was never retried and the rebuild
    published a second family beside it. Deletion status is now per pack and
    persisted, and only a live probe closes one out.
    """
    if not cfg.OLD_STATE.is_file():
        return True
    old = json.loads(cfg.OLD_STATE.read_text(encoding="utf-8"))
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
    png = cfg.EMOJI / f"{marker['key']}.png"
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
            if hamming(want, got) <= cfg.SAME_IMAGE_MAX:
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
    marker = state["in_flight"]
    known_sets = {s["name"] for s in state["sets"]}
    landed = False
    if marker.get("operation") == "create":
        set_state, sset = tg.probe_set_state(marker["set_name"])
        if set_state is SetState.UNKNOWN:
            _stop_retryable(f"live state of {marker['set_name']} is unknown; "
                            f"cannot tell whether the in-flight create landed")
        landed = set_state is SetState.EXISTS
        if landed:
            # EXISTENCE IS NOT IDENTITY. A set of that name can pre-exist -- a
            # leftover from an earlier family, or someone else's -- and
            # adopting it on the name alone attaches this rebuild's state, and
            # every later add, to a pack we never created. Our create put our
            # image in first, so that is what proves it.
            png = cfg.EMOJI / f"{marker['key']}.png"
            stickers = sset.get("stickers") or []
            # SHAPE FIRST, before anything is mutated. A create leaves EXACTLY
            # one sticker, so [OURS, FOREIGN] is not our set -- and it passes
            # the first-sticker check below. The count disagreement only
            # surfaced afterwards, in the cum/order comparison, by which time
            # state["sets"], the cursor and the order record had been written
            # and saved: a refusal that had already half-applied itself.
            if len(stickers) != 1:
                # Reachable in ordinary operation, not just in theory: the coin
                # providers publish into this same family, so a rebuild that
                # died mid-create followed by a provider top-up leaves
                # [OURS, PROVIDER]. Without an executable repair this refusal is
                # itself a permanent stop -- the shape the legacy-marker refusal
                # above was written to avoid.
                _stop_retryable(
                    f"{marker['set_name']} exists but holds {len(stickers)} "
                    f"sticker(s); the create that made it leaves exactly one, "
                    f"so this is not the set this rebuild created. Inspect that "
                    f"pack: if its first sticker IS {marker['key']}.png, another "
                    f"tool added to a set this rebuild had just made -- record "
                    f"it by hand in 'sets' (index {marker.get('set_index')}, "
                    f"name {marker['set_name']}), set 'in_flight' to null and "
                    f"append {marker['key']!r} to 'order'. If it is NOT ours, "
                    f"the name collides with another pack: rename this family's "
                    f"base, or delete that set, then re-run")
            same = tg._sticker_matches(stickers[0], png) if png.is_file() else None
            if same is False:
                _stop_retryable(
                    f"{marker['set_name']} exists but its first sticker is not "
                    f"{png.name}; refusing to adopt a set this rebuild did not "
                    f"create")
            if same is not True:
                _stop_retryable(
                    f"{marker['set_name']} exists but its first sticker could "
                    f"not be compared with {png.name}; refusing to adopt a set "
                    f"on an unverified identity")
        if landed and marker["set_name"] not in known_sets:
            live = len(sset.get("stickers", []))
            index = marker.get("set_index") or len(state["sets"]) + 1
            state["sets"].append({"index": index, "name": marker["set_name"],
                                  "title": f"{cfg.TITLE} {index}", "live": live})
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
    with exclusive_lock(cfg.LOCK):
        _build(tg, bot)


def _build(tg: Telegram, bot: str) -> None:
    plan = load_plan()
    # Validated against the plan BEFORE the delete phase: a state that cannot be
    # trusted must never get as far as destroying the existing packs.
    state = load_state(plan)
    state.setdefault("order", [])  # actual successful-upload order (drift-proof map)

    if not cfg.USER_ID:
        raise SystemExit("ERROR: PACK_OWNER_USER_ID is not set (env or .env); "
                         "refusing to start a rebuild without a pack owner.")
    # Validate BEFORE destroying anything. An empty or unusable plan (missing
    # emoji directory, unreadable plan file) would otherwise delete every
    # existing pack and then have nothing to rebuild them from.
    if not plan:
        raise SystemExit(
            f"ERROR: the rebuild plan is empty ({cfg.PLAN.name}); nothing to build.\n"
            f"       Expected prepared 100x100 PNGs in {cfg.EMOJI}.\n"
            f"       Refusing to delete the existing packs.")

    # Resume position comes from the RECORDED cursor, never from the live
    # sticker count. A plan entry that is skipped (missing / blank / failed)
    # consumes a plan position but produces no sticker, so `sum(live)` drifts
    # behind the plan index by one per skip -- resuming at plan[sum(live)] then
    # re-uploads entries that are already published, which is exactly how
    # duplicates and the mis-aligned ticker map were produced.
    #
    # This reconciliation runs BEFORE the delete phase on purpose. It reads only
    # the NEW sets, so it does not depend on the old packs -- and a state that
    # disagrees with live Telegram must never get as far as destroying them.
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

    # This rebuild is not the only writer to the family. The providers
    # (fetch_paprika / fetch_cmc) top it up with coins the frozen plan never
    # had, and their uploads CANNOT be recorded in `order` -- `order` must be a
    # subsequence of the plan prefix the cursor walked, which a new coin is not.
    # They keep their own tally in the same state file instead, so the invariant
    # is unchanged -- every live sticker is accounted for by some record -- and
    # only the arithmetic learns about the second writer.
    topped_up = state.get("provider_added") or []
    recorded = len(state["order"]) + len(topped_up)
    if cum != recorded:
        extra = (f" ({len(state['order'])} by this rebuild + {len(topped_up)} "
                 f"topped up by the providers)") if topped_up else ""
        raise SystemExit(
            f"ERROR: {cum} stickers are live but {recorded} "
            f"uploads are recorded{extra}.\n"
            f"       Refusing to continue: the recorded order is what maps "
            f"stickers to tickers, and continuing from a disagreeing state is "
            f"what corrupts ticker_to_id.json.\n"
            f"       Reconcile with coins/remap_ids.py, or delete the packs and "
            f"the state file to rebuild cleanly.")

    print(f"resume: {len(state['sets'])} sets, {cum} live stickers, "
          f"plan position {state['cursor']}/{len(plan)}", flush=True)

    if not state.get("deleted_old"):
        print(f"deleting ALL old packs and rebuilding {len(plan)} images...",
              flush=True)
        if not delete_old_packs(tg, state):
            _stop_retryable("some old packs still exist; refusing to build a "
                            "second family beside them")
        state["deleted_old"] = True
        save_state(state)

    if state["sets"] and state["sets"][-1]["live"] < cfg.PER_SET:
        cur = state["sets"][-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index, set_name, in_set = len(state["sets"]), "", 0

    for plan_i in range(state["cursor"], len(plan)):
        g = plan[plan_i]
        png = cfg.EMOJI / f"{g['rep']}.png"
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
                    tg.add_sticker(cfg.USER_ID, set_name, png, cfg.EMOJI_CHAR, kw,
                                   expected_before=in_set)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                set_index += 1
                set_name = f"{cfg.BASE}{set_index}_by_{bot}"
                title = f"{cfg.TITLE} {set_index}"
                _mark_in_flight(state, g["rep"], "create", set_name, set_index, 0)
                tg.create_set(cfg.USER_ID, set_name, title, png, cfg.EMOJI_CHAR, kw)
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
        if in_set >= cfg.PER_SET:
            notify(tg, state, set_name, f"{cfg.TITLE} {set_index}")
            in_set = 0
        time.sleep(0.1)

    if state["sets"]:
        last = state["sets"][-1]
        notify(tg, state, last["name"], last["title"])



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
