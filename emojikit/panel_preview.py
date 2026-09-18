"""Small, cached panel previews; source media and publication bytes are read only."""
from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs

from PIL import Image

from emojikit import media, video_decode

log = logging.getLogger("panel")

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
# A viewport can request a hundred uncached animations at once. Limit expensive
# raster/codec work while the independent HTTP save handlers remain responsive.
#
# The bound was a hard-coded 2, which on any real machine is the drip that made
# a cold tier arrive in visible chunks: measured here, one animation costs
# ~155 ms and one video poster ~613 ms, so 442 animations at two-at-a-time is
# ~34 s of rendering. Resource-aware instead, and still leaving most of the
# machine to the OS, the save handlers and whatever else the owner is running.
_renders = threading.BoundedSemaphore(max(2, min(6, (os.cpu_count() or 4) - 2)))


def parameters(query: str, maximum_fps: int) -> tuple[bool, int, int]:
    values = parse_qs(query, max_num_fields=4)
    fps = int(values.get("fps", [maximum_fps])[0])
    size = int(values.get("size", [104])[0])
    if size not in {52, 72, 104} or not 1 <= fps <= 30:
        raise ValueError("invalid preview size or frame rate")
    return values.get("still") == ["1"], min(fps, maximum_fps), size


def warm(view: list[dict], by_key: dict, db_path: Path, fps: int,
         size: int = 104, stop: threading.Event | None = None) -> int:
    """Render, in grid order, the previews the page is about to ask for.

    Every miss used to be paid at scroll time, one viewport at a time, behind
    the render bound -- which is what "the animations arrive in pieces" was.
    Warming in grid order means the top of the list is ready first and the rest
    lands before the owner scrolls that far. Rendering is best effort: a failure
    here must never take the panel down, because the request path renders the
    same file again anyway and reports its own error.
    """
    done = 0
    for card in view:
        if stop is not None and stop.is_set():
            break
        key, src = card.get("key"), by_key.get(card.get("key"))
        if not key or src is None or card.get("isLogo"):
            continue
        wanted = [(True, fps)] if card.get("fmt") != "animated" else [(True, fps), (False, fps)]
        for still, rate in wanted:
            if stop is not None and stop.is_set():
                break
            try:
                preview_bytes(key, Path(src), db_path, rate, still, size)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the warm-up
                log.debug("preview warm-up skipped %s (still=%s): %s", key, still, exc)
    return done


def preview_bytes(key: str, src: Path, db_path: Path, fps: int,
                  still: bool = False, size: int = 104) -> bytes:
    cache = db_path.parent / "preview"
    legacy = cache / f"{key.replace(':', '_')}@{'still' if still else fps}.webp"
    if (size == 104 and src.suffix.lower() == ".tgs"
            and legacy.parent.resolve() == cache.resolve() and legacy.is_file()):
        return legacy.read_bytes()
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()
    dest = cache / f"{name}@{'still' if still else fps}-{size}.webp"
    if dest.is_file():
        return dest.read_bytes()
    with _guard:
        lock = _locks.setdefault(str(dest), threading.Lock())
    with lock:
        if not dest.is_file():
            with _renders:
                if src.suffix.lower() == ".tgs":
                    if still:
                        media.lottie_still_webp(src, dest, size=size)
                    else:
                        media.lottie_preview_webp(src, dest, fps=fps, size=size)
                else:
                    cache.mkdir(parents=True, exist_ok=True)
                    if src.suffix.lower() == ".webm":
                        cmd = [media.ffmpeg_path(), "-v", "error", *video_decode.decoder_args(src),
                               "-i", str(src), "-frames:v", "1", "-an", "-threads", "1",
                               "-vf", f"scale={size}:{size},format=rgba", "-f", "rawvideo", "-"]
                        raw = media._run(cmd, capture=True).stdout
                        frame = Image.frombytes("RGBA", (size, size), raw)
                    else:
                        with Image.open(src) as image:
                            frame = image.convert("RGBA")
                            frame.thumbnail((size, size))
                    with frame:
                        buf = io.BytesIO()
                        frame.save(buf, "WEBP", lossless=True, exact=True)
                    tmp = dest.with_suffix(".tmp.webp")
                    tmp.write_bytes(buf.getvalue())
                    tmp.replace(dest)
        return dest.read_bytes()
