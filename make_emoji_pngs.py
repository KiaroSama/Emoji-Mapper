"""Convert images into Telegram custom-emoji ready PNGs (exactly 100x100).

Telegram custom emoji require a PNG of EXACTLY 100x100 px (RGBA, transparent
background). This script produces those from your source images.

Two modes:

1. General mode (any emoji pack):
     python make_emoji_pngs.py --in input/myset --out build/myset
   Reads every image in ``--in`` (.svg via resvg; .png/.jpg/.jpeg/.webp/.gif
   via Pillow) and writes ``<name>.png`` (100x100) into ``--out``. When several
   files share a name (foo.svg, foo.png) they are tried in ``SOURCE_PRIORITY``
   order, not file order, and the next one is used if the preferred source
   renders broken or blank.

2. Legacy crypto-coin mode (default, no --in/--out):
     python make_emoji_pngs.py
   Reads ``logos/svg/<ticker>.svg`` and ``logos/png/<ticker>.png`` and writes
   ``logos/emoji/<ticker>.png``.

Exit codes are the shared ones from build_pack: 0 nothing failed, 2 bad
arguments, 3 some sources failed, 4 every attempted source failed.

Re-running is cheap but not blind: an output is reused only when it really is a
100x100 RGBA non-blank PNG that is newer than its source. Edit a source and the
next run reconverts it.

Hang protection: before converting a file its name is written to a marker file
and cleared afterwards, so the marker doubles as a per-file heartbeat for
run_convert.ps1. If this process is killed while stuck, the next run reads the
marker, quarantines that name (``.svg_skip.txt`` in the output dir) and moves
on -- quarantined names are reported on every run so they can be reviewed.
(The renderer that used to spin has been replaced by resvg, but the guard is
kept as cheap insurance.)
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import resvg_py
from PIL import Image

from build_pack import EXIT_USAGE, ingest_exit_code

ROOT = Path(__file__).resolve().parent
# Legacy crypto-coin defaults (used when --in/--out are not provided).
SVG_DIR = ROOT / "logos" / "svg"
PNG_DIR = ROOT / "logos" / "png"
OUT_DIR = ROOT / "logos" / "emoji"
SIZE = 100
RENDER = 256
# Explicit source priority: several files can share one output name (foo.svg and
# foo.png both build foo.png), and the winner used to be whichever the directory
# listing happened to yield first. Vector master first, then lossless rasters,
# then lossy ones. An extension not listed here is not a source.
SOURCE_PRIORITY = (".svg", ".png", ".webp", ".gif", ".bmp", ".jpg", ".jpeg")
RASTER_EXTS = frozenset(SOURCE_PRIORITY) - {".svg"}


def _load_skip(skip: Path, marker: Path) -> set[str]:
    s = set()
    if skip.is_file():
        s.update(t.strip().lower() for t in skip.read_text(encoding="utf-8").splitlines() if t.strip())
    # If a previous run was killed mid-render, quarantine the culprit it recorded.
    if marker.is_file():
        culprit = marker.read_text(encoding="utf-8").strip().lower()
        if culprit:
            s.add(culprit)
            with open(skip, "a", encoding="utf-8") as fh:
                fh.write(culprit + "\n")
            print(f"QUARANTINE: '{culprit}' was interrupted mid-conversion; "
                  f"recorded in {skip.name} for review.", flush=True)
        marker.unlink(missing_ok=True)
    return s


def _report_quarantine(names: set[str], skip: Path) -> None:
    if names:
        print(f"REVIEW: {len(names)} source(s) quarantined and skipped: "
              f"{', '.join(sorted(names))}. Fix them and delete their lines from "
              f"{skip} to retry.", flush=True)


def _source_groups(in_dir: Path) -> list[list[Path]]:
    """Every source file per output name, best first (SOURCE_PRIORITY).

    The losers are kept, not discarded: the preferred source can render broken
    or blank (a gradient-only SVG, a clipped-away document), and the sibling
    raster is then the only way that name gets an emoji at all.
    """
    groups: dict[str, list[Path]] = {}
    for p in sorted(in_dir.iterdir()):
        if p.suffix.lower() not in SOURCE_PRIORITY or not p.is_file():
            continue
        groups.setdefault(p.stem.lower(), []).append(p)
    for g in groups.values():
        g.sort(key=lambda s: SOURCE_PRIORITY.index(s.suffix.lower()))
    return [groups[n] for n in sorted(groups)]


def _pick_sources(in_dir: Path) -> list[Path]:
    """The preferred source per output name (the one tried first)."""
    return [g[0] for g in _source_groups(in_dir)]


def _output_ok(out: Path, src: Path) -> bool:
    """True if ``out`` is already a usable emoji built from the current ``src``.

    Existence proves nothing: a run killed mid-save leaves a truncated PNG, an
    older pipeline may have written a differently sized one, and a source edited
    after its conversion has to be converted again.
    """
    try:
        if out.stat().st_mtime < src.stat().st_mtime:
            return False  # source changed since the emoji was written
        with Image.open(out) as im:
            return im.size == (SIZE, SIZE) and im.mode == "RGBA" and not _is_blank(im)
    except Exception:  # noqa: BLE001 - missing, truncated or unreadable -> rebuild
        return False


def _trim(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    bbox = img.split()[3].getbbox()
    return img.crop(bbox) if bbox else img


def _fit_100(img: Image.Image) -> Image.Image:
    img = _trim(img)
    w, h = img.size
    if w == 0 or h == 0:
        return Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    scale = min(SIZE / w, SIZE / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((SIZE - nw) // 2, (SIZE - nh) // 2), img)
    return canvas


def _render_svg(path: Path) -> Image.Image | None:
    """Rasterize an SVG to an RGBA image ``RENDER`` px on its longest side.

    resvg renders straight to RGBA. The previous backend had no alpha channel,
    so it rendered twice (on white, on black) and solved for alpha per pixel;
    it also could not paint gradients, silently producing a blank image.
    """
    try:
        png = resvg_py.svg_to_bytes(svg_path=str(path), width=RENDER)
    except Exception:  # noqa: BLE001 - a broken SVG must not stop the batch
        return None
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def _is_blank(img: Image.Image, min_visible: int = 8) -> bool:
    """True if an RGBA image is effectively empty (too few non-transparent pixels).

    Counted through the alpha histogram (buckets 11..255 == "a > 10") rather than
    a per-pixel Python loop: every existing output is now checked on every run,
    so 10k pixels per image adds up over a large emoji folder.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.getchannel("A")
    if alpha.getbbox() is None:
        return True
    return sum(alpha.histogram()[11:]) <= min_visible


def _convert_svg(p: Path, out: Path) -> bool:
    """Render an SVG to a 100x100 PNG. Returns True only on a NON-blank result.

    An SVG can still rasterize to nothing (empty document, everything clipped
    away). We never save such a blank result -- returning False lets the caller
    fall back to a raster source (logos/png/<ticker>.png) instead of producing a
    blank emoji.
    """
    img = _render_svg(p)
    if img is None:
        return False
    fitted = _fit_100(img)
    if _is_blank(fitted):
        return False
    fitted.save(out, format="PNG", optimize=True)
    return True


def _convert_raster(p: Path, out: Path) -> bool:
    """Open a raster image and fit it into a 100x100 transparent PNG.

    Returns False (without saving) if the result is blank, so a blank source can
    never become a blank emoji.
    """
    fitted = _fit_100(Image.open(p).convert("RGBA"))
    if _is_blank(fitted):
        return False
    fitted.save(out, format="PNG", optimize=True)
    return True


def _run_general(in_dir: Path, out_dir: Path, limit: int) -> int:
    """Convert every supported image in a single folder to 100x100 PNGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".svg_cur"
    skip = out_dir / ".svg_skip.txt"
    quarantined = _load_skip(skip, marker)
    made = svg_ok = raster_ok = failed = 0

    for group in _source_groups(in_dir):
        if limit and made >= limit:
            break
        name = group[0].stem.lower()
        out = out_dir / f"{name}.png"
        # ponytail: the reuse scan is silent to run_convert.ps1's heartbeat at
        # ~1.5 ms/output; only a folder of ~80k finished emojis would out-wait
        # its per-file deadline. Write the marker while checking if that day comes.
        # Every source must be older than the output, or editing the fallback
        # source alone would never be picked up.
        if name in quarantined or all(_output_ok(out, s) for s in group):
            continue
        marker.write_text(name, encoding="utf-8")  # heartbeat + culprit if we hang
        try:
            for src in group:
                is_svg = src.suffix.lower() == ".svg"
                try:
                    ok = (_convert_svg(src, out) if is_svg
                          else _convert_raster(src, out))
                except Exception:  # noqa: BLE001 - try the next source instead
                    ok = False
                if ok:
                    made += 1
                    if is_svg:
                        svg_ok += 1
                    else:
                        raster_ok += 1
                    break
            else:
                # Only when EVERY source for this name broke or rendered blank;
                # a blank source must never become a blank emoji.
                failed += 1
        finally:
            marker.unlink(missing_ok=True)
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, raster={raster_ok})", flush=True)

    _report_quarantine(quarantined, skip)
    total = len(list(out_dir.glob("*.png")))
    print(f"DONE: made {made} this run (svg={svg_ok}, raster={raster_ok}, failed={failed}); "
          f"total emoji PNGs in {out_dir}: {total}.", flush=True)
    return ingest_exit_code(made, failed)


def _run_legacy(limit: int) -> int:
    """Original crypto-coin pipeline: logos/svg + logos/png -> logos/emoji."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    marker = ROOT / "logos" / ".svg_cur"
    skip = ROOT / "logos" / ".svg_skip.txt"
    quarantined = _load_skip(skip, marker)
    done: set[str] = set()
    # Failure is per OUTPUT STEM, not per source attempt -- exactly as general
    # mode counts it. Counting the SVG attempt immediately reported a run where
    # logos/png/<t>.png then produced a perfectly good emoji as PARTIAL, and the
    # launcher/CI treated that healthy run as retryable.
    failed_stems: set[str] = set()
    made = svg_ok = png_ok = 0

    # SVG first, then PNG for the same ticker: same priority as SOURCE_PRIORITY.
    for p in sorted(SVG_DIR.glob("*.svg")):
        if limit and made >= limit:
            break
        t = p.stem.lower()
        out = OUT_DIR / f"{t}.png"
        if _output_ok(out, p):
            done.add(t)
            continue
        if t in quarantined:
            continue
        marker.write_text(t, encoding="utf-8")  # heartbeat + culprit if we hang here
        try:
            if _convert_svg(p, out):
                done.add(t)
                made += 1
                svg_ok += 1
            else:
                failed_stems.add(t)
        except Exception:  # noqa: BLE001
            failed_stems.add(t)
        finally:
            marker.unlink(missing_ok=True)
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, png={png_ok})", flush=True)

    # No quarantine check here on purpose: a quarantined name means its SVG hung,
    # and the raster fallback is exactly how that ticker still gets an emoji.
    for p in sorted(PNG_DIR.glob("*.png")):
        if limit and made >= limit:
            break
        t = p.stem.lower()
        if t in done:
            continue
        out = OUT_DIR / f"{t}.png"
        if _output_ok(out, p):
            done.add(t)
            failed_stems.discard(t)
            continue
        try:
            if _convert_raster(p, out):
                done.add(t)
                made += 1
                png_ok += 1
                failed_stems.discard(t)   # the fallback source carried this stem
            else:
                failed_stems.add(t)  # blank/empty source -> skip, no blank emoji
        except Exception:  # noqa: BLE001
            failed_stems.add(t)
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, png={png_ok})", flush=True)

    _report_quarantine(quarantined, skip)
    failed = len(failed_stems)
    total = len(list(OUT_DIR.glob("*.png")))
    print(f"DONE: made {made} this run (svg={svg_ok}, png={png_ok}, failed={failed}); "
          f"total emoji PNGs: {total}.", flush=True)
    return ingest_exit_code(made, failed)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build 100x100 Telegram custom-emoji PNGs.")
    ap.add_argument("--in", dest="in_dir", default="",
                    help="Source folder of mixed images (general mode).")
    ap.add_argument("--out", dest="out_dir", default="",
                    help="Output folder for 100x100 PNGs (general mode).")
    ap.add_argument("--limit", type=int, default=0, help="Max images this run (0=all).")
    args = ap.parse_args()

    # A negative limit is not "no limit": ``made >= limit`` holds before the
    # first conversion, so the run breaks out immediately and still exits 0.
    if args.limit < 0:
        print(f"ERROR: --limit must be 0 or greater (got {args.limit}).")
        return EXIT_USAGE

    if args.in_dir:
        in_dir = Path(args.in_dir)
        if not in_dir.is_dir():
            print(f"ERROR: --in folder not found: {in_dir}")
            return EXIT_USAGE
        out_dir = Path(args.out_dir) if args.out_dir else in_dir.parent / f"{in_dir.name}_emoji"
        return _run_general(in_dir, out_dir, args.limit)
    return _run_legacy(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
