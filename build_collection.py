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

# --- Brand logo (first emoji of every set built with the Emoji Mapper bot) --- #
# Only packs published by these bots get the mandatory YourBrand logo as their
# first emoji. The coin bot (@YourCoinEmojiBot) is intentionally
# excluded, so it is NOT in this set.
BRAND_LOGO_BOTS = {"youremojibot"}
BRAND_LOGO_DEFAULT = r"F:\documents\My Logo\YourBrand\YourBrand Emoji Logo.png"
BRAND_LOGO_EMOJI = "\u2705"          # ✅ associated standard emoji for the logo
BRAND_LOGO_KW = ["yourbrand", "logo"]


class BrandLogo:
    """Provides a format-matched copy of the brand logo for a set's first emoji.

    The source is a raster PNG. It is converted on demand and cached under
    ``<data_dir>/brand/``:

    * ``static``   -> ``logo.png``  (fit to 100x100)
    * ``video``    -> ``logo.webm`` (looped still, VP9/alpha, <=256 KB)
    * ``animated`` -> ``logo.tgs``  ONLY if a Lottie (.tgs/.json) source sits
      next to the PNG; a raster image cannot be turned into a vector .tgs, so
      animated sets are skipped (with a one-time warning) when none exists.
    """

    def __init__(self, src: str, data_dir: Path) -> None:
        self.src = Path(src) if src else None
        self.dir = data_dir / "brand"
        self._cache: dict[str, Path | None] = {}
        self._warned: set[str] = set()

    def available(self) -> bool:
        return bool(self.src and self.src.is_file())

    def _lottie_source(self) -> Path | None:
        if not self.src:
            return None
        for ext in (".tgs", ".json"):
            cand = self.src.with_suffix(ext)
            if cand.is_file():
                return cand
        return None

    def for_format(self, fmt: str) -> Path | None:
        """Return a ready-to-upload logo path for *fmt*, or None if impossible."""
        if not self.available():
            return None
        if fmt in self._cache:
            return self._cache[fmt]
        from emojikit import media
        result: Path | None = None
        try:
            if fmt == "static":
                out = self.dir / "logo.png"
                if not out.is_file():
                    media.to_static_png(self.src, out)
                result = out
            elif fmt == "video":
                out = self.dir / "logo.webm"
                if not out.is_file():
                    media.to_video_webm(self.src, out, loop_still=True, seconds=1.5)
                result = out
            elif fmt == "animated":
                lottie = self._lottie_source()
                if lottie:
                    out = self.dir / "logo.tgs"
                    if not out.is_file():
                        media.to_animated_tgs(lottie, out)
                    result = out
                elif "animated" not in self._warned:
                    log.warning("brand logo: no Lottie (.tgs/.json) next to %s; "
                                "animated packs will NOT get the logo first "
                                "(a raster image can't become a vector .tgs).",
                                self.src.name)
                    self._warned.add("animated")
        except Exception as exc:  # noqa: BLE001 - logo is best-effort, never fatal
            log.warning("brand logo for %s failed: %s", fmt, exc)
            result = None
        self._cache[fmt] = result
        return result


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


def _media_ok(path: Path, fmt: str) -> bool:
    """False if the media is effectively blank (guards against blank emoji)."""
    if fmt == "static":
        return not _static_is_blank(path)
    if fmt == "video":
        # Best-effort: a fully-transparent/empty first frame => treat as blank.
        try:
            from emojikit.media import _first_video_frame
            im = _first_video_frame(path)
            a = im.convert("RGBA").split()[3]
            if a.getbbox() is None:
                return False
            return sum(1 for v in a.getdata() if v > 10) > 8
        except Exception:  # noqa: BLE001 - probing failed; let the upload decide
            return True
    return True  # animated (.tgs) validity is enforced at creation time


def write_manifest(data_dir: Path, cat: Catalog, s: dict) -> None:
    """Write a per-pack manifest: emoji name (keywords) + custom_emoji_id."""
    keys = s.get("keys") or []
    if not keys:
        return
    md = data_dir / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    lines = [f"# {s.get('title', s['name'])}", "",
             f"Pack: https://t.me/addemoji/{s['name']}  |  format: {s['fmt']}  |  {len(keys)} emoji",
             "", "| # | Name | Emoji ID |", "|---|------|----------|"]
    for i, key in enumerate(keys, 1):
        it = cat.get(key)
        name = ", ".join(it.keywords[:2]) if it and it.keywords else (
            it.sources[0] if it and it.sources else key)
        cid = (it.custom_emoji_id if it else "") or ""
        lines.append(f"| {i} | {name} | {cid} |")
    (md / f"{s['name']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("manifest written: manifests/%s.md (%d emoji)", s["name"], len(keys))


def publish_format(tg: Telegram, cat: Catalog, *, fmt: str, plan_keys: list[str],
                   base: str, title: str, user_id: int, default_emoji: str,
                   per_set: int, data_dir: Path, state: dict, bot: str,
                   logo: "BrandLogo | None" = None) -> None:
    """Publish all pending items of one format into per-format sets.

    Duplicate-proof: "already uploaded" is decided by the catalog's per-item
    (committed) ``uploaded`` flag, NOT by a positional offset, so skipped items
    can never shift the boundary and cause a re-upload on resume.
    """
    if not plan_keys:
        return
    fmt_sets = [s for s in state["sets"] if s["fmt"] == fmt]
    skipped = set(state.setdefault("skipped", []))

    # Active (last, non-full) set: reconcile its capacity from the LIVE count.
    if fmt_sets:
        fmt_sets[-1]["live"] = live_count(tg, fmt_sets[-1]["name"])
    if fmt_sets and fmt_sets[-1]["live"] < per_set:
        cur = fmt_sets[-1]
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index = max((s["index"] for s in fmt_sets), default=0)
        set_name, in_set = "", 0

    # Pending = plan keys neither already uploaded, excluded in the panel, nor
    # permanently skipped.
    pending = [k for k in plan_keys
               if (it := cat.get(k)) and not it.uploaded and it.included
               and k not in skipped]
    log.info("[%s] %d sets, active in_set=%d, %d pending (of %d planned)",
             fmt, len(fmt_sets), in_set, len(pending), len(plan_keys))

    def skip(key: str, why: str) -> None:
        log.warning("[%s] skip %s: %s", fmt, key, why)
        skipped.add(key)
        state["skipped"] = sorted(skipped)
        save_json(_state_path(data_dir, base), state)

    n = 0
    for key in pending:
        item = cat.get(key)
        path = Path(item.file_path)
        if not path.is_file() or path.stat().st_size == 0:
            skip(key, "missing/empty media")
            continue
        if not _media_ok(path, fmt):
            skip(key, "blank media (no blank emoji)")
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
                logo_path = logo.for_format(fmt) if logo else None
                if logo_path:
                    # Brand logo is ALWAYS the first emoji of the set.
                    tg.create_emoji_set(user_id, set_name, set_title, logo_path,
                                        fmt, [BRAND_LOGO_EMOJI], BRAND_LOGO_KW)
                else:
                    tg.create_emoji_set(user_id, set_name, set_title, path, fmt,
                                        emojis, item.keywords)
                fmt_sets.append({"fmt": fmt, "index": set_index, "name": set_name,
                                 "title": set_title, "live": 1 if logo_path else 0,
                                 "logo": bool(logo_path), "keys": []})
                state["sets"].append(fmt_sets[-1])
                save_json(_state_path(data_dir, base), state)
                log.info("[%s set %d] created %s%s", fmt, set_index, set_name,
                         " (brand logo first)" if logo_path else "")
                if logo_path:
                    # Set now exists with the logo at position 0; protect it from
                    # index rollback, then place this item as the second sticker.
                    in_set = 1
                    tg.add_emoji(user_id, set_name, path, fmt, emojis, item.keywords)
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            # Transient/non-blank failure: log and retry on a later run (NOT
            # added to skipped), while the catalog flag keeps it dup-proof.
            log.warning("[%s] upload failed for %s (will retry): %s", fmt, key, redact(str(exc)))
            continue
        in_set += 1
        fmt_sets[-1]["live"] = in_set
        # Record actual upload order (for cid mapping) + mark uploaded (committed
        # immediately -> crash-safe duplicate guard).
        fmt_sets[-1].setdefault("keys", []).append(key)
        cat.mark_uploaded(key, None)
        n += 1
        if n % 20 == 0:
            save_json(_state_path(data_dir, base), state)
        if in_set >= per_set:
            notify(tg, user_id, state, data_dir, base, set_name,
                   f"{title} {FMT_WORD[fmt]} {set_index}")
            in_set = 0
        time.sleep(0.1)

    save_json(_state_path(data_dir, base), state)
    # Assign real custom_emoji_ids (drift-proof, from recorded order) + manifests.
    _record_cids(tg, cat, fmt_sets)
    for s in fmt_sets:
        write_manifest(data_dir, cat, s)

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
        offset = 1 if s.get("logo") else 0  # skip the brand logo at position 0
        for i, key in enumerate(keys):
            j = i + offset
            if j < len(live):
                cat.mark_uploaded(key, str(live[j].get("custom_emoji_id")))


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
    ap.add_argument("--brand-logo", default=BRAND_LOGO_DEFAULT,
                    help="Logo image used as the FIRST emoji of every set built "
                         "by the Emoji Mapper bot (ignored for the coin bot).")
    ap.add_argument("--no-brand-logo", action="store_true",
                    help="Disable the mandatory first-emoji brand logo.")
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

        # The YourBrand logo is the mandatory first emoji of every set built by
        # the Emoji Mapper bot; the coin bot is excluded by design.
        logo = None
        if not args.no_brand_logo and bot.lower() in BRAND_LOGO_BOTS:
            logo = BrandLogo(args.brand_logo, data_dir)
            if logo.available():
                log.info("brand logo enabled (first emoji of every set): %s",
                         args.brand_logo)
            else:
                log.warning("brand logo requested but not found: %s", args.brand_logo)
                logo = None

        state = load_json(_state_path(data_dir, base), {"base": base, "sets": [], "sent": []})
        state.setdefault("sets", []); state.setdefault("sent", [])

        for fmt in formats:
            publish_format(tg, cat, fmt=fmt, plan_keys=plan.get(fmt, []),
                           base=base, title=args.title, user_id=args.user_id,
                           default_emoji=args.emoji, per_set=args.per_set,
                           data_dir=data_dir, state=state, bot=bot, logo=logo)
        save_json(_state_path(data_dir, base), state)

    print("\nDONE.", flush=True)
    for s in state["sets"]:
        print(f"  https://t.me/addemoji/{s['name']}  [{s['fmt']}]", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
