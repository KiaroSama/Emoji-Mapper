"""Download *specific* premium custom-emoji by their IDs into the catalog.

Unlike ``fetch_pack.py`` (which pulls whole sticker sets), this downloads only
the individual custom-emoji you ask for — for example the ``premium-id:<n>``
entries found in bot inventory files — and nothing else from their packs.

Pipeline:

1. Collect custom-emoji IDs from ``--ids-file`` (one ID per line, or any text
   containing ``premium-id:<digits>``) and/or positional ``--id`` arguments.
2. **De-duplicate the IDs** (a warning is logged for every repeated ID) so each
   real emoji is fetched at most once — this is the whole point of the tool.
3. Resolve them with ``getCustomEmojiStickers`` (batched, max 200/call). Any ID
   Telegram can no longer resolve is reported as "missing".
4. Download each sticker, detect its format (static / animated / video),
   content-hash it and store it in the content-addressed catalog. Identical
   media (two different IDs pointing at the same file) collapse onto one entry.

Usage:
  python fetch_emoji_ids.py --ids-file ids.txt [--id 123 --id 456]
                            [--token-env GENERAL_BOT_TOKEN]
                            [--data-dir collection] [--phash-threshold -1]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path

from build_pack import Telegram, load_env
from emojikit import media
from emojikit.catalog import Catalog, DEFAULT_PHASH_THRESHOLD
from emojikit.logsetup import redact, setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("fetch_emoji_ids")


# A real inventory entry is a line whose first token (after an optional bullet)
# is ``premium-id:<n>``. Any label/emoji *after* the id is allowed, so formats
# like ``premium-id:123 👋 waving`` still count. Mentions in the middle of prose
# such as ``(e.g. premium-id: 123)`` are NOT real entries and are skipped, so
# example IDs are never fetched by mistake.
_REAL_ENTRY = re.compile(r"^\s*[-*]?\s*premium-id\s*:\s*(\d{5,25})(?!\d)", re.IGNORECASE)
# Any premium-id: mention anywhere on a line (used only to report skipped prose).
_ANY_MENTION = re.compile(r"premium-id\s*:\s*(\d{5,25})(?!\d)", re.IGNORECASE)
# A bare numeric ID on its own line (fallback for plain ID lists).
_BARE_ID = re.compile(r"^\s*(\d{5,25})\s*$")


def extract_real_ids(text: str) -> list[str]:
    """Return every real ``premium-id:`` entry ID in *text*, in order, WITH
    duplicates preserved. Prose/example mentions are excluded. If the text has
    no real entry lines, falls back to bare numeric-ID lines (plain ID lists).

    Pure and side-effect free so the duplicate logic can be unit-tested.
    """
    lines = text.splitlines()
    found = [m.group(1) for line in lines
             for m in [_REAL_ENTRY.match(line)] if m]
    if not found:
        found = [m.group(1) for line in lines
                 for m in [_BARE_ID.match(line)] if m]
    return found


def within_file_duplicates(text: str) -> dict[str, int]:
    """IDs that appear as a real entry more than once within the same text."""
    counts: dict[str, int] = {}
    for eid in extract_real_ids(text):
        counts[eid] = counts.get(eid, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def collect_ids(ids_files: list[str], inline_ids: list[str]) -> list[str]:
    """Read IDs from files and CLI, dedup, preserve first-seen order.

    Only real ``premium-id:<n>`` entry lines are taken (prose/example mentions
    are skipped); files that are plain lists of bare IDs are also supported.
    Within-file duplicates, cross-file duplicates and skipped example mentions
    are all logged for transparency.
    """
    ordered: list[str] = []
    occurrences: dict[str, int] = {}

    def _add(eid: str) -> None:
        occurrences[eid] = occurrences.get(eid, 0) + 1
        if eid not in ordered:
            ordered.append(eid)

    for f in ids_files:
        text = Path(f).read_text(encoding="utf-8", errors="replace")
        found = extract_real_ids(text)
        wdups = within_file_duplicates(text)
        # Mentions that are NOT real entries (prose/examples) we deliberately skip.
        all_mentions = len(_ANY_MENTION.findall(text))
        skipped = max(0, all_mentions - len(found))
        for eid in found:
            _add(eid)
        msg = f"{Path(f).name}: {len(found)} real entries ({len(set(found))} unique)"
        if skipped:
            msg += f", {skipped} example/prose mention(s) skipped"
        if wdups:
            msg += ", within-file duplicate(s): " + ", ".join(
                f"{k}x{v}" for k, v in sorted(wdups.items(), key=lambda kv: -kv[1]))
        log.info(msg)

    for eid in inline_ids:
        eid = eid.strip()
        if eid.isdigit():
            _add(eid)

    dups = {k: v for k, v in occurrences.items() if v > 1}
    total = sum(occurrences.values())
    log.info("collected %d real id occurrences -> %d unique (%d ids duplicated across files)",
             total, len(ordered), len(dups))
    if dups:
        top = sorted(dups.items(), key=lambda kv: -kv[1])[:10]
        log.info("duplicate ids (fetched once each): %s",
                 ", ".join(f"{k}x{v}" for k, v in top))
    return ordered


def _media_path(data_dir: Path, fmt: str, content_key: str, ext: str | None = None) -> Path:
    safe = content_key.replace(":", "_")
    return data_dir / "media" / fmt / f"{safe}{ext or media.ext_for_format(fmt)}"


def fetch_ids(tg: Telegram, cat: Catalog, ids: list[str], data_dir: Path,
              tmp_dir: Path) -> dict[str, int]:
    """Resolve + download the given unique IDs; return run counts."""
    counts = {"new": 0, "dedup": 0, "failed": 0, "missing": 0}

    # 1) Resolve every ID to its Sticker object (batches of 200).
    resolved: dict[str, dict] = {}
    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]
        stickers = tg.get_custom_emoji_stickers(batch)
        for st in stickers:
            cid = str(st.get("custom_emoji_id", ""))
            if cid:
                resolved[cid] = st
        log.info("resolved %d/%d ids so far", len(resolved), len(ids))

    missing = [i for i in ids if i not in resolved]
    counts["missing"] = len(missing)
    if missing:
        log.warning("%d id(s) could not be resolved by Telegram: %s",
                    len(missing), ", ".join(missing[:20]))

    # 2) Download + ingest each resolved sticker.
    for n, (cid, st) in enumerate(resolved.items(), 1):
        fuid = str(st.get("file_unique_id", ""))
        emoji = st.get("emoji")
        emojis = [emoji] if emoji else []
        # Keep provenance: the source premium id lives in keywords for traceability.
        keywords = [f"premium-id:{cid}"]

        known = cat.seen_file_unique_id(fuid) if fuid else None
        if known:
            cat.merge_labels(known, emojis=emojis, keywords=keywords,
                             source="bot-inventory", file_unique_id=fuid)
            counts["dedup"] += 1
            continue

        fmt = media.telegram_sticker_format(st)
        tmp = tmp_dir / f"emoji_{cid}.dl"
        try:
            tg.download_file(st["file_id"], tmp)
            key = media.content_key(tmp, fmt)
            phash = media.perceptual_hash(tmp, fmt)
            ext = media.media_extension(tmp, fmt)
            dest = _media_path(data_dir, fmt, key, ext)
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp.replace(dest)
            else:
                tmp.unlink(missing_ok=True)
            _, is_new = cat.add(content_key=key, fmt=fmt, file_path=dest,
                                emojis=emojis, keywords=keywords,
                                source="bot-inventory", phash=phash,
                                file_unique_id=fuid)
            counts["new" if is_new else "dedup"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad emoji must not stop the run
            log.warning("emoji %s failed: %s", cid, exc)
            counts["failed"] += 1
            tmp.unlink(missing_ok=True)
        if n % 25 == 0:
            log.info("  downloaded %d/%d", n, len(resolved))

    return counts


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("fetch_emoji_ids")
    ap = argparse.ArgumentParser(description="Download specific custom-emoji by ID.")
    ap.add_argument("--ids-file", action="append", default=[],
                    help="File with premium-id:<n> lines or bare IDs (repeatable).")
    ap.add_argument("--id", action="append", default=[], dest="inline_ids",
                    help="A single custom-emoji ID (repeatable).")
    ap.add_argument("--token-env", default="GENERAL_BOT_TOKEN",
                    help="Env var holding the bot token (default GENERAL_BOT_TOKEN).")
    ap.add_argument("--data-dir", default="collection", help="Catalog/media directory.")
    ap.add_argument("--phash-threshold", type=int, default=DEFAULT_PHASH_THRESHOLD,
                    help="Hamming distance for near-duplicate merging (-1 disables).")
    args = ap.parse_args(argv)

    if not args.ids_file and not args.inline_ids:
        log.error("provide --ids-file and/or --id")
        return 2

    ids = collect_ids(args.ids_file, args.inline_ids)
    if not ids:
        log.error("no valid custom-emoji IDs found")
        return 2

    token = os.environ.get(args.token_env, "")
    if not token:
        log.error("%s not set (env or .env).", args.token_env)
        return 2

    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    tmp_dir = data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tg = Telegram(token)
    try:
        bot = tg.get_me().get("username", "?")
        log.info("Authenticated bot: @%s", bot)
    except Exception as exc:  # noqa: BLE001
        log.error("getMe failed: %s", redact(str(exc)))
        return 2

    with Catalog(data_dir / "catalog.db", phash_threshold=args.phash_threshold) as cat:
        counts = fetch_ids(tg, cat, ids, data_dir, tmp_dir)
        stats = cat.stats()

    # Clean the scratch download directory (keep the catalog + media).
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    if tmp_dir.exists() and not any(tmp_dir.iterdir()):
        tmp_dir.rmdir()

    log.info("TOTAL: unique_ids=%d new=%d dedup=%d failed=%d missing=%d",
             len(ids), counts["new"], counts["dedup"], counts["failed"], counts["missing"])
    print(f"Done. unique_ids={len(ids)} new={counts['new']} dedup={counts['dedup']} "
          f"failed={counts['failed']} missing={counts['missing']}", flush=True)
    for fmt, s in sorted(stats.items()):
        print(f"  catalog {fmt}: {s['total']} total ({s['pending']} pending upload)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
