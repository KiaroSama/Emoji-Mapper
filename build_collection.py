"""Publish the catalog into new Telegram custom-emoji packs (multi-format).

Reads pending emoji from the content-addressed catalog and uploads them into
fresh custom-emoji sets owned by the configured user, with a new base name and
title. Static, animated and video emoji are published into SEPARATE sets,
because Telegram does not allow mixing formats within one set.

Duplicate-proof & resumable:

* A FROZEN, append-only plan (``publish_plan_<base>.json``) fixes the upload
  order per format, so resume is deterministic.
* Progress is reconciled from LIVE Telegram counts (sum of stickers actually
  present), exactly like the coin rebuild tool -> interrupting and resuming can
  never create a duplicate. ``pending = plan[fmt][live_total:]``.

Usage:
  python build_collection.py --base mypack --title "My Pack" \
      [--token-env GENERAL_BOT_TOKEN] [--user-id N] [--emoji 😀] \
      [--formats static,video,animated] [--per-set 200] [--data-dir collection] \
      [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

from build_pack import Telegram, load_env
from emojikit.catalog import Catalog
from emojikit.logsetup import redact, setup_logging
from PIL import Image

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("build_collection")

PER_SET = 200                       # Telegram custom-emoji set hard cap
FMT_TAG = {"static": "s", "video": "v", "animated": "a"}
FMT_WORD = {"static": "Static", "video": "Video", "animated": "Animated"}
DEFAULT_EMOJI = "\U0001F600"


def _static_is_blank(path: Path, min_visible: int = 8) -> bool:
    """True if a static image is effectively empty (guards against blank emoji)."""
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 - non-static or unreadable: let upload decide
        return False
    alpha = im.split()[3]
    if alpha.getbbox() is None:
        return True
    return sum(1 for v in alpha.getdata() if v > 10) <= min_visible


def _state_path(data_dir: Path, base: str) -> Path:
    return data_dir / f"publish_{base}.json"


def _plan_path(data_dir: Path, base: str) -> Path:
    return data_dir / f"publish_plan_{base}.json"


def load_json(path: Path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def freeze_plan(cat: Catalog, data_dir: Path, base: str, formats: list[str]) -> dict:
    """Build/extend the frozen, append-only upload plan from the catalog.

    Existing order is preserved; only newly-catalogued keys are appended, so the
    already-uploaded prefix of every format stays stable across runs.
    """
    plan = load_json(_plan_path(data_dir, base), {})
    for fmt in formats:
        existing = plan.get(fmt, [])
        have = set(existing)
        # Catalog rows in deterministic content_key order (matches Catalog.pending).
        ordered = [it.content_key for it in _all_items(cat, fmt)]
        appended = [k for k in ordered if k not in have]
        plan[fmt] = existing + appended
        if appended:
            log.info("plan[%s]: %d existing + %d new = %d", fmt, len(existing),
                     len(appended), len(plan[fmt]))
    save_json(_plan_path(data_dir, base), plan)
    return plan


def _all_items(cat: Catalog, fmt: str):
    """All catalog items of a format in deterministic order (uploaded or not)."""
    rows = cat.db.execute(
        "SELECT * FROM items WHERE format=? ORDER BY content_key", (fmt,)
    ).fetchall()
    from emojikit.catalog import _row_to_item  # local import to avoid cycle noise
    return [_row_to_item(r) for r in rows]


def live_count(tg: Telegram, name: str) -> int:
    try:
        return len(tg.get_sticker_set(name).get("stickers", []))
    except Exception:  # noqa: BLE001
        return 0


def notify(tg: Telegram, user_id: int, state: dict, data_dir: Path, base: str,
           name: str, title: str) -> None:
    if name in state["sent"]:
        return
    try:
        tg.send_message(user_id, f"\u2705 {title}\nhttps://t.me/addemoji/{name}")
        state["sent"].append(name)
        save_json(_state_path(data_dir, base), state)
        log.info("sent link for %s", name)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify failed for %s: %s", name, exc)


def publish_format(tg: Telegram, cat: Catalog, *, fmt: str, plan_keys: list[str],
                   base: str, title: str, user_id: int, default_emoji: str,
                   per_set: int, data_dir: Path, state: dict, bot: str) -> None:
    """Publish all pending items of one format into per-format sets."""
    if not plan_keys:
        return
    fmt_sets = [s for s in state["sets"] if s["fmt"] == fmt]

    # Reconcile from live counts -> duplicate-proof resume.
    cum = 0
    for s in fmt_sets:
        s["live"] = live_count(tg, s["name"])
        cum += s["live"]
    log.info("[%s] %d sets, %d live, %d pending", fmt, len(fmt_sets), cum,
             len(plan_keys) - cum)

    if fmt_sets and fmt_sets[-1]["live"] < per_set:
        cur = fmt_sets[-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index = max((s["index"] for s in fmt_sets), default=0)
        set_name, in_set = "", 0

    for key in plan_keys[cum:]:
        item = cat.get(key)
        if item is None:
            log.warning("[%s] missing catalog item %s; skipping", fmt, key)
            continue
        path = Path(item.file_path)
        if not path.is_file() or path.stat().st_size == 0:
            log.warning("[%s] missing/empty media for %s; skipping", fmt, key)
            continue
        if fmt == "static" and _static_is_blank(path):
            log.warning("[%s] BLANK image for %s; skipping (no blank emoji)", fmt, key)
            continue
        emojis = item.emojis or [default_emoji]
        try:
            placed = False
            if in_set != 0:
                try:
                    tg.add_emoji(user_id, set_name, path, fmt, emojis, item.keywords)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                set_index += 1
                set_name = f"{base}{FMT_TAG[fmt]}{set_index}_by_{bot}"
                set_title = f"{title} {FMT_WORD[fmt]} {set_index}"
                tg.create_emoji_set(user_id, set_name, set_title, path, fmt,
                                    emojis, item.keywords)
                fmt_sets.append({"fmt": fmt, "index": set_index, "name": set_name,
                                 "title": set_title, "live": 0, "keys": []})
                state["sets"].append(fmt_sets[-1])
                save_json(_state_path(data_dir, base), state)
                log.info("[%s set %d] created %s", fmt, set_index, set_name)
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            log.warning("[%s] skip %s: %s", fmt, key, redact(str(exc)))
            continue
        in_set += 1
        fmt_sets[-1]["live"] = in_set
        # Record the ACTUAL upload order so the cid<->key mapping can never drift
        # (this is the root-cause fix for the historical scrambled map).
        fmt_sets[-1].setdefault("keys", []).append(key)
        cat.mark_uploaded(key, None)
        if in_set >= per_set:
            notify(tg, user_id, state, data_dir, base, set_name,
                   f"{title} {FMT_WORD[fmt]} {set_index}")
            in_set = 0
        time.sleep(0.1)

    save_json(_state_path(data_dir, base), state)
    # Assign real custom_emoji_ids from the live sets, matched by the recorded
    # upload order (drift-proof). Tickers/keys map to the exact sticker created.
    _record_cids(tg, cat, fmt_sets)

    if fmt_sets:
        last = fmt_sets[-1]
        notify(tg, user_id, state, data_dir, base, last["name"], last["title"])


def _record_cids(tg: Telegram, cat: Catalog, fmt_sets: list[dict]) -> None:
    """Store each item's real custom_emoji_id using the recorded upload order."""
    for s in fmt_sets:
        keys = s.get("keys") or []
        if not keys:
            continue
        try:
            live = tg.get_sticker_set(s["name"]).get("stickers", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s for cid mapping: %s", s["name"], exc)
            continue
        for i, key in enumerate(keys):
            if i < len(live):
                cat.mark_uploaded(key, str(live[i].get("custom_emoji_id")))


def valid_base(base: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", base):
        raise SystemExit("ERROR: --base must start with a letter and contain only "
                         "letters/digits (no underscores), e.g. 'mypack'.")
    return base


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("build_collection")
    ap = argparse.ArgumentParser(description="Publish the catalog into new emoji packs.")
    ap.add_argument("--base", required=True, help="Set-name base (letters/digits).")
    ap.add_argument("--title", required=True, help="Human-readable set title.")
    ap.add_argument("--token-env", default="GENERAL_BOT_TOKEN")
    ap.add_argument("--user-id", type=int, default=int(os.environ.get("PACK_OWNER_USER_ID", "0")))
    ap.add_argument("--emoji", default=DEFAULT_EMOJI, help="Fallback associated emoji.")
    ap.add_argument("--formats", default="static,video,animated",
                    help="Comma list of formats to publish, in order.")
    ap.add_argument("--per-set", type=int, default=PER_SET)
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    base = valid_base(args.base)
    formats = [f.strip() for f in args.formats.split(",") if f.strip() in FMT_TAG]
    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    db = data_dir / "catalog.db"
    if not db.is_file():
        log.error("no catalog at %s (run fetch_pack.py / add_media.py first).", db)
        return 2

    with Catalog(db) as cat:
        plan = freeze_plan(cat, data_dir, base, formats)
        stats = cat.stats()

        if args.dry_run:
            print("DRY RUN: nothing uploaded.", flush=True)
            for fmt in formats:
                keys = plan.get(fmt, [])
                n_sets = (len(keys) + args.per_set - 1) // args.per_set if keys else 0
                print(f"  {fmt}: {len(keys)} emoji -> {n_sets} set(s) "
                      f"named {base}{FMT_TAG[fmt]}1_by_<bot> ...", flush=True)
            return 0

        token = os.environ.get(args.token_env, "")
        if not token:
            log.error("%s not set (env or .env).", args.token_env)
            return 2
        if not args.user_id:
            log.error("provide --user-id or PACK_OWNER_USER_ID.")
            return 2

        tg = Telegram(token)
        bot = tg.get_me()["username"]
        log.info("Publishing as @%s, owner=%s", bot, args.user_id)

        state = load_json(_state_path(data_dir, base), {"base": base, "sets": [], "sent": []})
        state.setdefault("sets", []); state.setdefault("sent", [])

        for fmt in formats:
            publish_format(tg, cat, fmt=fmt, plan_keys=plan.get(fmt, []),
                           base=base, title=args.title, user_id=args.user_id,
                           default_emoji=args.emoji, per_set=args.per_set,
                           data_dir=data_dir, state=state, bot=bot)
        save_json(_state_path(data_dir, base), state)

    print("\nDONE.", flush=True)
    for s in state["sets"]:
        print(f"  https://t.me/addemoji/{s['name']}  [{s['fmt']}]", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
