"""Convert images into Telegram custom-emoji ready PNGs (exactly 100x100).

Telegram custom emoji require a PNG of EXACTLY 100x100 px (RGBA, transparent
background). This script produces those from your source images.

Two modes:

1. General mode (any emoji pack):
     python make_emoji_pngs.py --in input/myset --out build/myset
   Reads every image in ``--in`` (.svg via resvg; .png/.jpg/.jpeg/.webp/.gif
   via Pillow) and writes ``<name>.png`` (100x100) into ``--out``. When several
   files share a name (foo.svg, foo.png) the source is picked by
   ``SOURCE_PRIORITY``, not by file order.

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


def _pick_sources(in_dir: Path) -> list[Path]:
    """One source file per output name, chosen by SOURCE_PRIORITY."""
    best: dict[str, Path] = {}
    for p in sorted(in_dir.iterdir()):
        ext = p.suffix.lower()
        if ext not in SOURCE_PRIORITY or not p.is_file():
            continue
        cur = best.get(p.stem.lower())
        if cur is None or SOURCE_PRIORITY.index(ext) < SOURCE_PRIORITY.index(cur.suffix.lower()):
            best[p.stem.lower()] = p
    return [best[n] for n in sorted(best)]


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
    """True if an RGBA image is effectively empty (too few non-transparent pixels)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[3]
    if alpha.getbbox() is None:
        return True
    visible = sum(1 for a in alpha.get_flattened_data() if a > 10)
    return visible <= min_visible


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

    for p in _pick_sources(in_dir):
        if limit and made >= limit:
            break
        name = p.stem.lower()
        out = out_dir / f"{name}.png"
        if name in quarantined or _output_ok(out, p):
            continue
        marker.write_text(name, encoding="utf-8")  # heartbeat + culprit if we hang
        try:
            if p.suffix.lower() == ".svg":
                if _convert_svg(p, out):
                    made += 1; svg_ok += 1
                else:
                    failed += 1
            elif _convert_raster(p, out):
                made += 1; raster_ok += 1
            else:
                failed += 1  # blank/empty source -> never write a blank emoji
        except Exception:  # noqa: BLE001
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
    made = svg_ok = png_ok = failed = 0

    # SVG first, then PNG for the same ticker: same priority as SOURCE_PRIORITY.
    for p in sorted(SVG_DIR.glob("*.svg")):
        if limit and made >= limit:
            break
        t = p.stem.lower()
        out = OUT_DIR / f"{t}.png"
        if _output_ok(out, p):
            done.add(t); continue
        if t in quarantined:
            continue
        marker.write_text(t, encoding="utf-8")  # heartbeat + culprit if we hang here
        try:
            if _convert_svg(p, out):
                done.add(t); made += 1; svg_ok += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1
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
            done.add(t); continue
        try:
            if _convert_raster(p, out):
                done.add(t); made += 1; png_ok += 1
            else:
                failed += 1  # blank/empty source -> skip instead of blank emoji
        except Exception:  # noqa: BLE001
            failed += 1
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, png={png_ok})", flush=True)

    _report_quarantine(quarantined, skip)
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
