"""Build custom-emoji media from scratch and add it to the catalog.

Takes local source files (images, animations, videos, or Lottie JSON) and
converts each into a Telegram-compliant emoji, then deduplicates and stores it
in the content-addressed catalog -- ready to publish with ``build_collection.py``.

Target format selection (``--as auto`` by default):

* still image (.png/.jpg/.webp/.bmp/.svg, non-animated .gif)  -> **static** (PNG)
* animated raster (.gif animated, .apng, .mp4, .webm, .mov, ...) -> **video** (WEBM)
* Lottie vector (.json / .tgs)                                  -> **animated** (TGS)

Note: animated emoji (.tgs) are VECTOR only. A GIF/MP4 cannot become an animated
emoji -- it becomes a *video* emoji. Only Lottie JSON/TGS can be packaged as
animated.

Usage:
  python add_media.py --in input/myset [--as auto] [--emoji 😀]
                      [--keywords "tag1,tag2"] [--data-dir collection]
  python add_media.py file1.png clip.gif anim.json --emoji 🔥
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

from PIL import Image

from build_pack import ingest_exit_code
from emojikit import identity, media
from emojikit.catalog import Catalog, DEFAULT_PHASH_THRESHOLD, phash_threshold_arg
from emojikit.ingest import store_media
from emojikit.logsetup import record_exit_code, setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("add_media")

_STILL_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".svg"}
_LOTTIE_EXTS = {".json", ".tgs"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".apng"}


def _is_animated_gif(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            return getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1
    except Exception:  # noqa: BLE001
        return False


def choose_format(path: Path, requested: str) -> str:
    """Decide the target emoji format for a local source file."""
    if requested != "auto":
        return requested
    ext = path.suffix.lower()
    if ext in _LOTTIE_EXTS:
        return "animated"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == ".gif":
        return "video" if _is_animated_gif(path) else "static"
    if ext in _STILL_EXTS:
        return "static"
    # Unknown: sniff the bytes.
    fmt = media.detect_format(path)
    return fmt if fmt != "unknown" else "static"


def convert(path: Path, fmt: str, tmp_dir: Path) -> Path:
    """Convert a source file into the target format; returns the temp output."""
    out = tmp_dir / f"{path.stem}{media.ext_for_format(fmt)}"
    if fmt == "static":
        return media.to_static_png(path, out)
    if fmt == "video":
        media.to_video_webm(path, out)
        media.validate_video(out)
        return out
    if fmt == "animated":
        media.to_animated_tgs(path, out)
        return out
    raise media.MediaError(f"unsupported target format: {fmt}")


def _media_path(data_dir: Path, fmt: str, content_key: str) -> Path:
    safe = content_key.replace(":", "_")
    return data_dir / "media" / fmt / f"{safe}{media.ext_for_format(fmt)}"


def iter_sources(args) -> list[Path]:
    files: list[Path] = []
    if args.in_dir:
        d = Path(args.in_dir)
        if not d.is_dir():
            log.error("--in folder not found: %s", d)
            return []
        files.extend(sorted(p for p in d.iterdir() if p.is_file()))
    files.extend(Path(f) for f in args.files)
    return files


def main(argv: list[str] | None = None) -> int:
    # This script never loaded .env at all, so every setting configured there --
    # the ffmpeg timeout it leans on more than any other tool, the log retention
    # window, the secret values the logger masks -- was invisible to it. Before
    # setup_logging, so the masking sweep sees a loaded environment.
    from build_pack import load_env
    load_env()
    setup_logging("add_media")
    ap = argparse.ArgumentParser(description="Build emoji media from local files into the catalog.")
    ap.add_argument("files", nargs="*", help="Individual source files.")
    ap.add_argument("--in", dest="in_dir", default="", help="Folder of source files.")
    ap.add_argument("--as", dest="as_fmt", default="auto",
                    choices=["auto", "static", "video", "animated"],
                    help="Target format (default auto-detect).")
    ap.add_argument("--emoji", default="\U0001F600", help="Associated standard emoji.")
    ap.add_argument("--keywords", default="", help="Comma-separated extra keywords.")
    ap.add_argument("--data-dir", default="collection", help="Catalog/media directory.")
    ap.add_argument("--phash-threshold", type=phash_threshold_arg,
                    default=DEFAULT_PHASH_THRESHOLD,
                    help="Near-duplicate Hamming distance: -1 disables, else 0..16.")
    args = ap.parse_args(argv)

    sources = iter_sources(args)
    if not sources:
        log.error("no input files (use --in FOLDER or list files).")
        return 2

    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    tmp_dir = data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    extra_kw = [k.strip() for k in args.keywords.split(",") if k.strip()]

    counts = {"new": 0, "dedup": 0, "failed": 0}
    with Catalog(data_dir / "catalog.db", phash_threshold=args.phash_threshold) as cat, \
            tempfile.TemporaryDirectory(prefix="local-", dir=tmp_dir) as scratch:
        tmp_dir = Path(scratch)
        for src in sources:
            try:
                fmt = choose_format(src, args.as_fmt)
                tmp = convert(src, fmt, tmp_dir)
                # One decode for both keys: separately, a video paid two ffmpeg
                # launches over the same clip -- the priciest step in ingest,
                # doubled.
                key, phash = identity.fingerprint(tmp, fmt)
                dest = store_media(tmp, _media_path(data_dir, fmt, key), fmt, key,
                                   provenance={"source": "local", "name": src.name})
                kw = [src.stem.lower()] + extra_kw
                _, is_new = cat.add(content_key=key, fmt=fmt, file_path=dest,
                                    emojis=[args.emoji], keywords=kw,
                                    source=f"local:{src.name}", phash=phash)
                counts["new" if is_new else "dedup"] += 1
                log.info("%s -> %s (%s)%s", src.name, fmt, key,
                         "" if is_new else " [dup]")
            except Exception as exc:  # noqa: BLE001
                log.warning("failed %s: %s", src.name, exc)
                counts["failed"] += 1
        stats = cat.stats()

    print(f"Done. new={counts['new']} dedup={counts['dedup']} failed={counts['failed']}",
          flush=True)
    for fmt, s in sorted(stats.items()):
        print(f"  catalog {fmt}: {s['total']} total ({s['pending']} pending upload)", flush=True)
    return ingest_exit_code(counts["new"] + counts["dedup"], counts["failed"])


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
