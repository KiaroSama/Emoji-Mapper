"""Make a PUBLISHED pack match the order you arranged in the curate panel.

Reordering a live set does not re-upload anything: ``setStickerPositionInSet``
moves an existing sticker, so every emoji keeps its ``file_id`` AND its
``custom_emoji_id``. Anyone already using one is unaffected. That is what makes
this safe to run on a pack people have already installed -- and it is the reason
the panel's order is not frozen the moment you publish.

Two rules it will not bend:

* **Every live sticker must be identified first.** Positions are only meaningful
  if we know what is at each one; moving stickers around an unrecognised one
  would shuffle a stranger's emoji into the middle of the pack. An unknown
  sticker stops the run for that set.
* **The brand logo stays at position 0.** It leads the set by design.

``setStickerPositionInSet`` is idempotent -- setting the same sticker to the
same index twice leaves the same set -- so this is safe to re-run, and a run
interrupted halfway simply continues.

Usage:
  python sync_order.py --base mypack              # show what would move
  python sync_order.py --base mypack --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from emojikit.collection_state import (_lock_path, _state_path, load_state,
                              save_json)
from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_USAGE, load_env)
from emojikit.packstate import (LockBusy, exclusive_lock)
from emojikit.maintenance import writer
from emojikit.telegram_api import (Telegram)
from emojikit.catalog import Catalog
from emojikit.logsetup import record_exit_code, setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("sync_order")

# Telegram rate-limits set mutations. One move per ~120 ms keeps a 200-emoji
# pack under the limit without the flood waits a tighter loop earns.
MOVE_DELAY = 0.12


def desired_order(cat: Catalog, live_cids: set[str]) -> list[str]:
    """The custom_emoji_ids of this set, in the panel's order.

    Read from the catalog's ``position``, which IS the arrangement -- the same
    source the publisher used, so "sync" means "agree with the panel" and not
    "agree with some second copy of the order".
    """
    return [it.custom_emoji_id for it in cat.all_items()
            if it.custom_emoji_id and it.custom_emoji_id in live_cids]


def plan_moves(live: list[dict], desired: list[str],
               pinned: int) -> list[tuple[int, str, str]]:
    """(target_index, custom_emoji_id, file_id) moves that sort ``live``.

    Selection sort: each step puts the right sticker at the next index and
    every step is one API call, so an interrupted run leaves a set that is
    correct up to wherever it stopped rather than scrambled.
    """
    order = list(live)
    moves: list[tuple[int, str, str]] = []
    for offset, cid in enumerate(desired):
        target = pinned + offset
        if order[target].get("custom_emoji_id") == cid:
            continue
        j = next(i for i, st in enumerate(order)
                 if st.get("custom_emoji_id") == cid)
        moves.append((target, cid, order[j]["file_id"]))
        order.insert(target, order.pop(j))
    return moves


def sync_set(tg: Telegram, cat: Catalog, rec: dict, *, apply: bool) -> int:
    """Reorder one published set. Returns the number of moves (or planned)."""
    name = rec["name"]
    live = tg.get_sticker_set(name)["stickers"]
    known = {it.custom_emoji_id for it in cat.all_items() if it.custom_emoji_id}

    # The logo is the first sticker and is not a catalog item, so it is the one
    # legitimately unknown sticker -- and only at index 0.
    pinned = 1 if rec.get("logo") else 0
    strangers = [st.get("custom_emoji_id") for st in live[pinned:]
                 if st.get("custom_emoji_id") not in known]
    if strangers:
        log.error("%s holds %d sticker(s) this catalog does not know (%s...). "
                  "Refusing to reorder around them: positions are only "
                  "meaningful once every sticker is identified.",
                  name, len(strangers), strangers[0])
        return -1

    desired = desired_order(cat, {st.get("custom_emoji_id") for st in live[pinned:]})
    if len(desired) != len(live) - pinned:
        log.error("%s: %d live stickers but %d have a catalog position; "
                  "refusing to guess where the rest belong.",
                  name, len(live) - pinned, len(desired))
        return -1

    moves = plan_moves(live, desired, pinned)
    if not moves:
        log.info("%s: already in the panel's order (%d stickers).",
                 name, len(live))
    else:
        log.info("%s: %d of %d stickers need moving.", name, len(moves), len(live))
        for target, cid, file_id in moves:
            if not apply:
                log.info("  would move %s -> position %d", cid, target)
                continue
            tg.set_sticker_position(file_id, target)
            time.sleep(MOVE_DELAY)
    # Rewritten even when nothing moved: a set can be in the right order while
    # the RECORD of that order is stale -- which is exactly what an earlier
    # reorder left behind, and what blocks the next publish.
    if apply:
        # The publisher records the order it uploaded in and refuses to add to a
        # set whose live order no longer matches it -- by identity, position by
        # position. Reordering without rewriting that record leaves the whole
        # family unpublishable: the next publish stopped dead on "position 117
        # now holds a sticker this publisher cannot identify". The reorder is
        # legitimate, so the record follows it.
        key_by_cid = {it.custom_emoji_id: it.content_key
                      for it in cat.all_items() if it.custom_emoji_id}
        rec["keys"] = [key_by_cid[cid] for cid in desired]
    return len(moves)


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("sync_order")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="Pack family base name.")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--token-env", default="GENERAL_BOT_TOKEN")
    ap.add_argument("--pack", type=int, action="append", default=[],
                    metavar="N",
                    help="Only reorder pack N (repeatable). Default: every pack "
                         "in the family. A run that arranged ONE pack should "
                         "not move stickers in the others.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually move stickers. Without it, only report.")
    args = ap.parse_args(argv)

    data_dir = ROOT / args.data_dir if not Path(args.data_dir).is_absolute() \
        else Path(args.data_dir)
    db_path = data_dir / "catalog.db"
    if not db_path.is_file():
        log.error("no catalog at %s", db_path)
        return EXIT_USAGE
    if not _state_path(data_dir, args.base).is_file():
        log.error("no published state for base %r -- nothing to reorder.",
                  args.base)
        return EXIT_USAGE

    token = os.environ.get(args.token_env, "")
    if not token:
        log.error("%s not set (env or .env).", args.token_env)
        return EXIT_USAGE

    try:
        # Same lock as the publisher: reordering while a publish appends would
        # move stickers out from under it.
        with writer(data_dir), exclusive_lock(_lock_path(data_dir, args.base)):
            state = load_state(data_dir, args.base)
            sets = sorted(state.get("sets", []), key=lambda s: s["index"])
            if not sets:
                log.error("state for %r records no sets.", args.base)
                return EXIT_USAGE
            if args.pack:
                # Fail on an unknown number rather than silently reordering
                # nothing and reporting success.
                known = {s["index"] for s in sets}
                unknown = sorted(set(args.pack) - known)
                if unknown:
                    log.error("no pack %s in %r; it has %s.",
                              ", ".join(map(str, unknown)), args.base,
                              ", ".join(str(i) for i in sorted(known)))
                    return EXIT_USAGE
                sets = [s for s in sets if s["index"] in set(args.pack)]
                log.info("limited to pack %s",
                         ", ".join(str(s["index"]) for s in sets))
            tg = Telegram(token)
            cat = Catalog(db_path)
            try:
                total = failed = 0
                for rec in sets:
                    n = sync_set(tg, cat, rec, apply=args.apply)
                    if n < 0:
                        failed += 1
                    else:
                        total += n
                if args.apply:
                    # Written under the same lock that made the moves, so the
                    # record cannot be left describing a set it no longer
                    # matches.
                    save_json(_state_path(data_dir, args.base), state)
            finally:
                cat.close()
    except LockBusy as exc:
        log.error("%s", exc)
        return EXIT_FAILED

    verb = "moved" if args.apply else "would move"
    log.info("%s %d sticker(s) across %d set(s); %d set(s) skipped.",
             verb, total, len(sets), failed)
    if not args.apply and total:
        print(f"\n{total} sticker(s) are out of order. Re-run with --apply "
              f"to fix them.\n", flush=True)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
