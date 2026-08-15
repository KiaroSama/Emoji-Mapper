"""Download emoji from existing Telegram custom-emoji packs into the catalog.

Reads one or more source packs (by short name or ``t.me/addemoji/<name>`` link)
via the Bot API, downloads every sticker, detects its format (static / animated
/ video), deduplicates it by content, and stores it in the content-addressed
catalog. Re-running is cheap and safe:

* stickers whose Telegram ``file_unique_id`` was already ingested are skipped
  without downloading again;
* identical / near-identical media collapse onto a single catalog entry.

Usage:
  python fetch_pack.py PACK [PACK ...] [--token-env GENERAL_BOT_TOKEN]
                       [--data-dir collection] [--phash-threshold 5] [--limit N]

PACK may be a bare set name (``coolpack_by_somebot``) or a full
``https://t.me/addemoji/coolpack_by_somebot`` link.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from build_pack import Telegram, ingest_exit_code, load_env
from emojikit import media
from emojikit.catalog import Catalog, DEFAULT_PHASH_THRESHOLD
from emojikit.logsetup import record_exit_code, redact, setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("fetch_pack")


def pack_name(arg: str) -> str:
    """Normalize a pack argument (link or bare name) to its short name."""
    arg = arg.strip()
    for prefix in ("https://t.me/addemoji/", "http://t.me/addemoji/",
                   "t.me/addemoji/", "tg://addemoji?slug="):
        if arg.startswith(prefix):
            return arg[len(prefix):].split("?")[0].strip("/")
    return arg


def _media_path(data_dir: Path, fmt: str, content_key: str, ext: str | None = None) -> Path:
    safe = content_key.replace(":", "_")
    return data_dir / "media" / fmt / f"{safe}{ext or media.ext_for_format(fmt)}"


def fetch_one(tg: Telegram, cat: Catalog, name: str, data_dir: Path,
              tmp_dir: Path, limit: int = 0) -> dict[str, int]:
    """Ingest a single pack; returns counts of new/dedup/skipped/failed.

    ``limit`` bounds NEW catalog items, not stickers looked at: already-known
    stickers are skipped for free, so scanning past them is what makes
    ``--limit N`` actually deliver N new emoji on a re-run.
    """
    log.info("Fetching pack: %s", name)
    sset = tg.get_sticker_set(name)
    stickers = sset.get("stickers", [])
    title = sset.get("title", name)
    counts = {"new": 0, "dedup": 0, "skipped": 0, "failed": 0}

    for i, st in enumerate(stickers):
        if limit and counts["new"] >= limit:
            break
        fuid = str(st.get("file_unique_id", ""))
        emoji = st.get("emoji")
        emojis = [emoji] if emoji else []
        keywords = list(st.get("keywords", []))  # usually empty for foreign packs

        # Fast pre-dedup: already ingested this exact Telegram file -> no download.
        known = cat.seen_file_unique_id(fuid) if fuid else None
        if known:
            cat.merge_labels(known, emojis=emojis, keywords=keywords,
                             source=name, file_unique_id=fuid)
            counts["dedup"] += 1
            continue

        fmt = media.telegram_sticker_format(st)
        tmp = tmp_dir / f"{name}_{i}.dl"
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
                                emojis=emojis, keywords=keywords, source=name,
                                phash=phash, file_unique_id=fuid)
            counts["new" if is_new else "dedup"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad sticker must not stop the run
            log.warning("sticker %d of %s failed: %s", i, name, exc)
            counts["failed"] += 1
            tmp.unlink(missing_ok=True)
        if (i + 1) % 50 == 0:
            log.info("  %s: %d/%d processed", name, i + 1, len(stickers))

    log.info("Pack %s (%s): %d stickers -> new=%d dedup=%d failed=%d",
             name, title, len(stickers), counts["new"], counts["dedup"], counts["failed"])
    return counts


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("fetch_pack")
    ap = argparse.ArgumentParser(description="Download Telegram emoji packs into the catalog.")
    ap.add_argument("packs", nargs="+", help="Pack short names or addemoji links.")
    ap.add_argument("--token-env", default="GENERAL_BOT_TOKEN",
                    help="Env var holding the bot token (default GENERAL_BOT_TOKEN).")
    ap.add_argument("--data-dir", default="collection", help="Catalog/media directory.")
    ap.add_argument("--phash-threshold", type=int, default=DEFAULT_PHASH_THRESHOLD,
                    help="Hamming distance for near-duplicate merging (-1 disables).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max NEW catalog items per pack; already-known stickers "
                         "are skipped and do not count (0=all).")
    args = ap.parse_args(argv)

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

    total = {"new": 0, "dedup": 0, "failed": 0}
    with Catalog(data_dir / "catalog.db", phash_threshold=args.phash_threshold) as cat:
        for raw in args.packs:
            name = pack_name(raw)
            try:
                c = fetch_one(tg, cat, name, data_dir, tmp_dir, args.limit)
            except RuntimeError as exc:
                log.error("pack %s failed: %s", name, redact(str(exc)))
                continue
            for k in total:
                total[k] += c[k]
        stats = cat.stats()

    # Clean the scratch download directory (keep the catalog + media).
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    tmp_dir.rmdir() if not any(tmp_dir.iterdir()) else None

    log.info("TOTAL ingested: new=%d dedup=%d failed=%d", total["new"], total["dedup"], total["failed"])
    print(f"Done. new={total['new']} dedup={total['dedup']} failed={total['failed']}", flush=True)
    for fmt, s in sorted(stats.items()):
        print(f"  catalog {fmt}: {s['total']} total ({s['pending']} pending upload)", flush=True)
    return ingest_exit_code(total["new"] + total["dedup"], total["failed"])


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
