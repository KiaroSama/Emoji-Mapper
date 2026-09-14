"""Small, cached panel previews; source media and publication bytes are read only."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import threading
from urllib.parse import parse_qs

from PIL import Image

from emojikit import media, video_decode

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
# A viewport can request a hundred uncached animations at once. Limit expensive
# raster/codec work while the independent HTTP save handlers remain responsive.
_renders = threading.BoundedSemaphore(2)


def parameters(query: str, maximum_fps: int) -> tuple[bool, int, int]:
    values = parse_qs(query, max_num_fields=4)
    fps = int(values.get("fps", [maximum_fps])[0])
    size = int(values.get("size", [104])[0])
    if size not in {52, 72, 104} or not 1 <= fps <= 30:
        raise ValueError("invalid preview size or frame rate")
    return values.get("still") == ["1"], min(fps, maximum_fps), size


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
