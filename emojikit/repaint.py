"""Baking a `needs_repainting` look into artwork that does not carry the flag.

Split out of `emojikit.media`, which was over the project's size ceiling and
carries a different responsibility: media is detect/convert/validate, this is
one narrow transformation applied at ingest. `media` re-exports the three
public names so no caller changed.

A client repaints a `needs_repainting` sticker: the artwork is only a
silhouette and the colour comes from the theme. The Bot API will not let us ask
for that flag on an existing pack -- it appears on `Sticker` (read-only) and on
`createNewStickerSet` (whole-set, at creation) and nowhere else -- so for a pack
that already exists the only way to get the look is to bake it.

Three defects the audit's adjacent-path review turned up here, all silent:

* an ANIMATED colour (a keyframed `c.k`) was skipped entirely, so a file came
  back "repainted" with every one of its original colours intact;
* a gradient's OPACITY ramp was overwritten with colour values, because the
  stop walker strode through the whole array in fours without asking how many
  of those entries were colour stops;
* a colour carrying its own alpha had it forced to 1, which is the opposite of
  what the static branch documents -- it fills THROUGH the original alpha so
  cut-outs and antialiased edges survive.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger("emojikit.media")


def parse_tint(text: str) -> tuple[int, int, int]:
    """``#RRGGBB`` / ``RRGGBB`` -> (r, g, b). Raises MediaError on anything else."""
    from emojikit.media import MediaError
    s = text.strip().lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise MediaError(f"tint must be #RRGGBB, got {text!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _numeric(seq) -> bool:
    return isinstance(seq, list) and all(isinstance(v, (int, float)) for v in seq)


def _recolour(values: list, rgb: list[float]) -> None:
    """Replace r,g,b in place and LEAVE the fourth component alone.

    Lottie writes a colour as [r, g, b] or [r, g, b, a]. Overwriting the fourth
    entry with 1 made a half-transparent fill opaque, which contradicts the
    static branch of `repaint_in_place`: that one fills through the original
    alpha precisely so a cut-out stays a cut-out.
    """
    for i in range(min(3, len(values))):
        values[i] = rgb[i]


def _tint_gradient(grad: dict, rgb: list[float]) -> None:
    """Recolour a gradient's colour stops and never touch its opacity stops.

    Lottie packs both into ONE flat array: `p` colour stops of
    [offset, r, g, b], optionally followed by opacity stops of [offset, alpha].
    Walking the whole array in fours therefore ran off the end of the colour
    section and rewrote the opacity ramp as if it were colour -- measured:
    [0.0, 1.0, 1.0, 0.25] came back as [0.0, 0, 0, 1], so a gradient that faded
    out became fully opaque.
    """
    k = grad.get("k")
    if not isinstance(k, dict):
        return
    stops = k.get("k")
    if not _numeric(stops):
        return
    count = grad.get("p")
    if not isinstance(count, int) or count < 0:
        # No declared stop count: only the whole array being a clean multiple
        # of four makes "all of it is colour" a safe reading.
        count = len(stops) // 4 if len(stops) % 4 == 0 else 0
    limit = min(count * 4, len(stops))
    for base in range(0, limit - 3, 4):
        # Assign through the slice: `stops[a:b]` is a COPY, so mutating it
        # would change nothing. The offset at `base` is deliberately untouched,
        # which is what keeps the shape of the ramp.
        stops[base + 1:base + 4] = [rgb[0], rgb[1], rgb[2]]


def _tint_lottie(node, k: list[float]) -> None:
    """Rewrite every solid colour and gradient stop in a Lottie tree, in place."""
    rgb = list(k[:3])
    if isinstance(node, dict):
        colour = node.get("c")
        if isinstance(colour, dict):
            values = colour.get("k")
            if _numeric(values):
                _recolour(values, rgb)
            elif isinstance(values, list):
                # An ANIMATED colour: a list of keyframes, each holding its own
                # colour in `s` (and `e` in older exports). This branch did not
                # exist, so `repaint_in_place` returned True having changed
                # nothing -- a silent no-op under a name that promises a
                # repaint, which is exactly why the video format is refused
                # outright rather than quietly skipped.
                for frame in values:
                    if not isinstance(frame, dict):
                        continue
                    for field in ("s", "e"):
                        if _numeric(frame.get(field)):
                            _recolour(frame[field], rgb)
        grad = node.get("g")
        if isinstance(grad, dict):
            _tint_gradient(grad, rgb)
        for value in node.values():
            _tint_lottie(value, k)
    elif isinstance(node, list):
        for value in node:
            _tint_lottie(value, k)


def repaint_in_place(path: Path, fmt: str, rgb: tuple[int, int, int]) -> bool:
    """Flatten ``path`` to one colour, keeping its shape. True when rewritten.

    Animated goes through the Lottie tree rather than the raster, because
    flattening frames would throw the animation away. Static fills through the
    original alpha, so anti-aliased edges and cut-outs (the tick inside a
    verified badge is a HOLE, not a dark shape) both survive.

    Video is refused: there is no cheap colour-exact route, and a silent no-op
    would publish the untouched art under a name that says otherwise.

    Call this BEFORE fingerprinting -- like `reencode_in_place`, it moves the
    bytes and the content key must describe what is on disk.
    """
    from emojikit.media import TGS_MAX_BYTES, _load_lottie
    try:
        if fmt == "animated":
            data = _load_lottie(path)
            _tint_lottie(data, [c / 255 for c in rgb] + [1])
            raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
                gz.write(raw)
            out = buf.getvalue()
            if len(out) > TGS_MAX_BYTES:
                log.warning("repaint of %s would be %d bytes, over the %d cap; "
                            "keeping the original", path.name, len(out),
                            TGS_MAX_BYTES)
                return False
        elif fmt == "static":
            with Image.open(path) as im:
                rgba = im.convert("RGBA")
            flat = Image.new("RGBA", rgba.size, (*rgb, 255))
            flat.putalpha(rgba.getchannel("A"))
            buf = io.BytesIO()
            flat.save(buf, format="WEBP", lossless=True, quality=100,
                      method=6, exact=True)
            out = buf.getvalue()
        else:
            log.warning("repaint not supported for %s (%s)", path.name, fmt)
            return False
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        log.warning("repaint skipped for %s (%s): %s", path.name, fmt, exc)
        return False

    path.write_bytes(out)
    return True
