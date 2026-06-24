"""Convert images into Telegram custom-emoji ready PNGs (exactly 100x100).

Telegram custom emoji require a PNG of EXACTLY 100x100 px (RGBA, transparent
background). This script produces those from your source images.

Two modes:

1. General mode (any emoji pack):
     python make_emoji_pngs.py --in input/myset --out build/myset
   Reads every image in ``--in`` (.svg via svglib; .png/.jpg/.jpeg/.webp/.gif
   via Pillow) and writes ``<name>.png`` (100x100) into ``--out``.

2. Legacy crypto-coin mode (default, no --in/--out):
     python make_emoji_pngs.py
   Reads ``logos/svg/<ticker>.svg`` and ``logos/png/<ticker>.png`` and writes
   ``logos/emoji/<ticker>.png``.

Hang protection: some SVGs make svglib spin forever. Before rendering an SVG its
name is written to a marker file; if this process is killed while stuck, the
next run reads the marker, blacklists that name (``.svg_skip.txt`` in the output
dir) and moves on. Run via run_convert.ps1 which restarts until it completes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

ROOT = Path(__file__).resolve().parent
# Legacy crypto-coin defaults (used when --in/--out are not provided).
SVG_DIR = ROOT / "logos" / "svg"
PNG_DIR = ROOT / "logos" / "png"
OUT_DIR = ROOT / "logos" / "emoji"
SIZE = 100
RENDER = 256
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _load_skip(skip: Path, marker: Path) -> set[str]:
    s = set()
    if skip.is_file():
        s.update(t.strip().lower() for t in skip.read_text(encoding="utf-8").splitlines() if t.strip())
    # If a previous run was killed mid-render, blacklist the culprit it recorded.
    if marker.is_file():
        culprit = marker.read_text(encoding="utf-8").strip().lower()
        if culprit:
            s.add(culprit)
            with open(skip, "a", encoding="utf-8") as fh:
                fh.write(culprit + "\n")
        marker.unlink(missing_ok=True)
    return s


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
    drawing = svg2rlg(str(path))
    if drawing is None or not drawing.width or not drawing.height:
        return None
    scale = RENDER / max(drawing.width, drawing.height)
    drawing.scale(scale, scale)
    drawing.width *= scale
    drawing.height *= scale
    on_white = renderPM.drawToPIL(drawing, dpi=72, bg=0xFFFFFF).convert("RGB")
    on_black = renderPM.drawToPIL(drawing, dpi=72, bg=0x000000).convert("RGB")
    w = np.asarray(on_white, dtype=np.int16)
    b = np.asarray(on_black, dtype=np.int16)
    diff = (w - b).clip(0, 255).mean(axis=2)
    alpha = (255.0 - diff).clip(0, 255)
    a = alpha / 255.0
    with np.errstate(divide="ignore", invalid="ignore"):
        color = np.where(a[..., None] > 0.003, b / a[..., None], 0.0)
    rgba = np.dstack([color.clip(0, 255), alpha]).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def _is_blank(img: Image.Image, min_visible: int = 8) -> bool:
    """True if an RGBA image is effectively empty (too few non-transparent pixels)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[3]
    if alpha.getbbox() is None:
        return True
    visible = sum(1 for a in alpha.getdata() if a > 10)
    return visible <= min_visible


def _convert_svg(p: Path, out: Path) -> bool:
    """Render an SVG to a 100x100 PNG. Returns True only on a NON-blank result.

    Some SVG features (e.g. gradient fills) are not rendered by the bundled
    svglib/reportlab backend and yield a fully transparent image. We never save
    such a blank result -- returning False lets the caller fall back to a raster
    source (logos/png/<ticker>.png) instead of producing a blank emoji.
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
    blacklist = _load_skip(skip, marker)
    made = svg_ok = raster_ok = failed = 0

    files = sorted(p for p in in_dir.iterdir()
                   if p.is_file() and (p.suffix.lower() == ".svg"
                                       or p.suffix.lower() in RASTER_EXTS))
    for p in files:
        if limit and made >= limit:
            break
        name = p.stem.lower()
        out = out_dir / f"{name}.png"
        if out.exists() or name in blacklist:
            continue
        try:
            if p.suffix.lower() == ".svg":
                marker.write_text(name, encoding="utf-8")  # record culprit if we hang
                ok = _convert_svg(p, out)
                marker.unlink(missing_ok=True)
                if ok:
                    made += 1; svg_ok += 1
                else:
                    failed += 1
            else:
                if _convert_raster(p, out):
                    made += 1; raster_ok += 1
                else:
                    failed += 1  # blank/empty source -> never write a blank emoji
        except Exception:  # noqa: BLE001
            marker.unlink(missing_ok=True)
            failed += 1
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, raster={raster_ok})", flush=True)

    total = len(list(out_dir.glob("*.png")))
    print(f"DONE: made {made} this run (svg={svg_ok}, raster={raster_ok}, failed={failed}); "
          f"total emoji PNGs in {out_dir}: {total}.", flush=True)
    return 0


def _run_legacy(limit: int) -> int:
    """Original crypto-coin pipeline: logos/svg + logos/png -> logos/emoji."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    marker = ROOT / "logos" / ".svg_cur"
    skip = ROOT / "logos" / ".svg_skip.txt"
    blacklist = _load_skip(skip, marker)
    done: set[str] = set()
    made = svg_ok = png_ok = failed = 0

    for p in sorted(SVG_DIR.glob("*.svg")):
        if limit and made >= limit:
            break
        t = p.stem.lower()
        out = OUT_DIR / f"{t}.png"
        if out.exists():
            done.add(t); continue
        if t in blacklist:
            continue
        marker.write_text(t, encoding="utf-8")  # record culprit if we hang here
        try:
            if _convert_svg(p, out):
                done.add(t); made += 1; svg_ok += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1
        marker.unlink(missing_ok=True)
        if made and made % 250 == 0:
            print(f"  ...{made} emojis (svg={svg_ok}, png={png_ok})", flush=True)

    for p in sorted(PNG_DIR.glob("*.png")):
        if limit and made >= limit:
            break
        t = p.stem.lower()
        if t in done:
            continue
        out = OUT_DIR / f"{t}.png"
        if out.exists():
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

    total = len(list(OUT_DIR.glob("*.png")))
    print(f"DONE: made {made} this run (svg={svg_ok}, png={png_ok}, failed={failed}); "
          f"total emoji PNGs: {total}.", flush=True)
    return 0


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
            return 2
        out_dir = Path(args.out_dir) if args.out_dir else in_dir.parent / f"{in_dir.name}_emoji"
        return _run_general(in_dir, out_dir, args.limit)
    return _run_legacy(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
