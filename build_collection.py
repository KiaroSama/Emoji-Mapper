"""Publish the catalog into new Telegram custom-emoji packs (multi-format).

Reads pending emoji from the content-addressed catalog and uploads them into
fresh custom-emoji sets owned by the configured user, with a new base name and
title. Static, animated and video emoji are published into SEPARATE sets by
default for organization/clarity. NOTE: since Bot API 7.2 (March 2024) a single
set MAY contain mixed formats, so this split is a choice, not a requirement --
the brand logo (static) is added as the first emoji of every set regardless of
the set's format.

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

from build_pack import AmbiguousUploadError, Telegram, load_env
from emojikit import media
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
# Only packs published by these bots get the mandatory God Verify logo as their
# first emoji. The coin bot (@GodVerifyCoinEmojiMapperbot) is intentionally
# excluded, so it is NOT in this set.
BRAND_LOGO_BOTS = {"godverifyemojimapperbot"}
BRAND_LOGO_DEFAULT = r"F:\documents\My Logo\God Verify\God Verify Emoji Logo.png"
BRAND_LOGO_EMOJI = "\u2705"          # ✅ associated standard emoji for the logo
BRAND_LOGO_KW = ["godverify", "logo"]


class BrandLogo:
    """The brand logo (God Verify) used as the FIRST emoji of every set.

    Since Bot API 7.2 (March 2024) a single custom-emoji set may contain mixed
    formats, so the logo is always a **static** 100x100 PNG and can lead a
    static, video OR animated set alike. Prepared once and cached under
    ``<data_dir>/brand/logo.png``.
    """

    def __init__(self, src: str, data_dir: Path) -> None:
        self.src = Path(src) if src else None
        self.dir = data_dir / "brand"
        self._png: Path | None = None

    def available(self) -> bool:
        return bool(self.src and self.src.is_file())

    def static_png(self) -> Path | None:
        """Return a ready 100x100 PNG logo path, or None if unavailable."""
        if not self.available():
            return None
        if self._png and self._png.is_file():
            return self._png
        from emojikit import media
        out = self.dir / "logo.png"
        try:
            if not out.is_file():
                media.to_static_png(self.src, out)
            self._png = out
            return out
        except Exception as exc:  # noqa: BLE001 - logo is best-effort, never fatal
            log.warning("brand logo prepare failed: %s", exc)
            return None


def _static_is_blank(path: Path, min_visible: int = 8) -> bool:
    """True if a static image is effectively empty (guards against blank emoji)."""
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 - non-static or unreadable: let upload decide
        return False
    alpha = im.split()[3]
    if alpha.getbbox() is None:
        return True
    return sum(1 for v in alpha.get_flattened_data() if v > 10) <= min_visible


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
    # Publish in the manual curate-panel order (position), content_key as a
    # stable tiebreak, so each format's set follows the order you arranged.
    rows = cat.db.execute(
        "SELECT * FROM items WHERE format=? ORDER BY position, content_key", (fmt,)
    ).fetchall()
    from emojikit.catalog import _row_to_item  # local import to avoid cycle noise
    return [_row_to_item(r) for r in rows]


def _try_get_set(tg, name: str) -> dict | None:
    """The live StickerSet, or None if it is missing or state is unknown.

    Prefers the non-retrying probe (getStickerSet via ``_call`` treats
    STICKERSET_INVALID as a name-release lock and sleeps minutes, which a
    lookup of a possibly-nonexistent set must never do)."""
    probe = getattr(tg, "probe_sticker_set", None)
    if probe is not None:
        known, sset = probe(name)
        return sset if known else None
    try:  # test fakes without the probe
        return tg.get_sticker_set(name)
    except Exception:  # noqa: BLE001
        return None


def live_count(tg: Telegram, name: str) -> int:
    sset = _try_get_set(tg, name)
    return len(sset.get("stickers", [])) if sset else 0


def _resolve_sticker_key(tg, cat: Catalog, st: dict, tmp_dir: Path) -> str | None:
    """Map a LIVE sticker back to its catalog content_key.

    Fast path: its ``file_unique_id`` was recorded (ingest or a previous
    publish). Slow path: download the sticker and content-hash it. Returns
    None if it cannot be attributed to any catalog item."""
    fuid = str(st.get("file_unique_id") or "")
    if fuid:
        known = cat.seen_file_unique_id(fuid)
        if known and cat.get(known) is not None:
            return known
    file_id = st.get("file_id")
    if not file_id or not hasattr(tg, "download_file"):
        return None
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"reconcile_{fuid or 'unknown'}.dl"
    try:
        tg.download_file(file_id, tmp)
        key = media.content_key(tmp, media.telegram_sticker_format(st))
    except Exception as exc:  # noqa: BLE001 - unattributable, not fatal
        log.warning("reconcile download failed (%s): %s", fuid or file_id,
                    redact(str(exc)))
        return None
    finally:
        tmp.unlink(missing_ok=True)
    if cat.get(key) is None:
        return None
    if fuid:
        cat.record_file_unique_id(fuid, key)
    return key


def reconcile_set(tg, cat: Catalog, s: dict, data_dir: Path) -> int:
    """Sync one set's records with its LIVE stickers; returns the live count.

    Any live sticker beyond what the state recorded is an upload a previous
    run (or an ambiguous network failure in this run) applied without
    recording it. Each one is attributed back to its catalog item by
    file_unique_id or downloaded content and marked uploaded, so pending
    computations can NEVER upload it a second time."""
    sset = _try_get_set(tg, s["name"])
    if sset is None:
        return int(s.get("live") or 0)
    live = sset.get("stickers", [])
    keys = s.setdefault("keys", [])
    offset = 1 if s.get("logo") else 0
    for st in live[offset + len(keys):]:
        key = _resolve_sticker_key(tg, cat, st, data_dir / "tmp")
        if key is None:
            # keys are positional (cid mapping): never attribute past a
            # sticker we cannot recognize (e.g. added manually by the owner).
            log.warning("[%s] unrecognized live sticker in %s at position %d; "
                        "stopping attribution there", s.get("fmt", "?"),
                        s["name"], offset + len(keys))
            break
        item = cat.get(key)
        if item is not None and not item.uploaded:
            log.info("[%s] reconciled from live: %s was already uploaded to %s",
                     s.get("fmt", "?"), key, s["name"])
        cat.mark_uploaded(key, str(st.get("custom_emoji_id") or "") or None)
        fuid = str(st.get("file_unique_id") or "")
        if fuid:
            cat.record_file_unique_id(fuid, key)
        keys.append(key)
    s["live"] = len(live)
    return len(live)


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
            return sum(1 for v in a.get_flattened_data() if v > 10) > 8
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

    # Reconcile the ACTIVE (last, non-full) set against its LIVE content
    # before anything else: uploads applied by a previous run but never
    # recorded (crash or ambiguous network failure) are attributed back to
    # their catalog items here, so the pending computation below can never
    # upload them a second time. This also refreshes the live capacity.
    if fmt_sets:
        reconcile_set(tg, cat, fmt_sets[-1], data_dir)
        save_json(_state_path(data_dir, base), state)
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
        if item is None or item.uploaded:
            continue  # attributed by an in-run reconcile after an ambiguous failure
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
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
                                 item.keywords, expected_before=in_set)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                set_index += 1
                set_name = f"{base}{FMT_TAG[fmt]}{set_index}_by_{bot}"
                set_title = f"{title} {FMT_WORD[fmt]} {set_index}"
                logo_png = logo.static_png() if logo else None
                adopted = False
                try:
                    if logo_png:
                        # Brand logo is ALWAYS the first emoji of the set. Mixed-format
                        # sets are allowed (Bot API 7.2+), so a STATIC logo can lead a
                        # static, video or animated set alike.
                        tg.create_emoji_set(user_id, set_name, set_title, logo_png,
                                            "static", [BRAND_LOGO_EMOJI], BRAND_LOGO_KW)
                    else:
                        tg.create_emoji_set(user_id, set_name, set_title, path, fmt,
                                            emojis, item.keywords)
                except RuntimeError as exc:
                    # If an earlier attempt of THIS name actually landed (network
                    # failure after apply, or a leftover from a crashed run), the
                    # set exists: adopt it instead of erroring forever on
                    # "name is already occupied" or re-creating it.
                    if not (isinstance(exc, AmbiguousUploadError)
                            or "occupied" in str(exc).lower()):
                        raise
                    if _try_get_set(tg, set_name) is None:
                        raise
                    adopted = True
                fmt_sets.append({"fmt": fmt, "index": set_index, "name": set_name,
                                 "title": set_title, "live": 1 if logo_png else 0,
                                 "logo": bool(logo_png), "keys": []})
                state["sets"].append(fmt_sets[-1])
                save_json(_state_path(data_dir, base), state)
                log.info("[%s set %d] %s %s%s", fmt, set_index,
                         "adopted" if adopted else "created", set_name,
                         " (brand logo first)" if logo_png else "")
                if adopted:
                    # Attribute whatever the set already contains (the earlier
                    # create put SOMETHING there), then re-check this item.
                    in_set = reconcile_set(tg, cat, fmt_sets[-1], data_dir)
                    save_json(_state_path(data_dir, base), state)
                    if cat.get(key).uploaded:
                        continue  # this very item was the set's first sticker
                    if in_set > (1 if logo_png else 0) + len(fmt_sets[-1]["keys"]):
                        log.warning("[%s] %s has unattributed live stickers; not "
                                    "adding %s yet (will retry)", fmt, set_name, key)
                        continue
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
                                 item.keywords, expected_before=in_set)
                elif logo_png:
                    # Set now exists with the logo at position 0; protect it from
                    # index rollback, then place this item as the second sticker.
                    in_set = 1
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
                                 item.keywords, expected_before=in_set)
        except AmbiguousUploadError as exc:
            # The add/create may or may not be live. NEVER blind-retry (that is
            # exactly how the same emoji lands in a pack twice) -- reconcile the
            # live set now; if the item did land it gets marked uploaded, and
            # if not it stays pending for a later run.
            log.warning("[%s] %s for %s; reconciling live set", fmt, exc, key)
            if not placed and in_set == 0:
                set_index -= 1  # the create never registered a set
            elif fmt_sets:
                in_set = reconcile_set(tg, cat, fmt_sets[-1], data_dir)
                save_json(_state_path(data_dir, base), state)
            continue
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
                # Remember the uploaded copy's file_unique_id: a later fetch of
                # our own published pack (or of ids inside it) is then caught by
                # the fast pre-dedup and never downloaded again.
                fuid = str(live[j].get("file_unique_id") or "")
                if fuid:
                    cat.record_file_unique_id(fuid, key)


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

        # The God Verify logo is the mandatory first emoji of every set built by
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
