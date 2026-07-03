"""Media handling for Telegram custom emoji: detect, hash and convert.

Telegram custom emoji come in three formats (see https://core.telegram.org/stickers
and the Bot API ``InputSticker.format`` field):

============  =========  ===========================================  ==========
format        extension  nature                                       hard caps
============  =========  ===========================================  ==========
``static``    .png/.webp 100x100 image (RGBA)                          -
``animated``  .tgs       gzip-compressed Lottie (vector) animation     <=64 KB
``video``     .webm      VP9 video, 100x100, <=3 s, 30 fps, no audio    <=256 KB
============  =========  ===========================================  ==========

This module provides:

* :func:`detect_format` -- decide static/animated/video from magic bytes.
* :func:`content_key` / :func:`perceptual_hash` -- deduplication keys.
* :func:`to_static_png` / :func:`to_video_webm` / :func:`to_animated_tgs` --
  conversion ("build from scratch") into each format.
* validation helpers backed by ffprobe.

Raster/vector image decoding uses Pillow (+ optional svglib for SVG). Video and
animated-video work requires ``ffmpeg``/``ffprobe`` on PATH.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger("emojikit.media")

SIZE = 100                       # custom emoji canvas (px)
TGS_MAX_BYTES = 64 * 1024        # animated emoji hard cap
WEBM_MAX_BYTES = 256 * 1024      # video emoji hard cap
WEBM_MAX_SECONDS = 3.0
WEBM_FPS = 30

# Container/codec magic bytes used for fast format sniffing.
_GZIP_MAGIC = b"\x1f\x8b"
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"      # Matroska/WebM
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".apng"}
VIDEO_SOURCE_EXTS = {".webm", ".mp4", ".mov", ".mkv", ".gif", ".apng", ".m4v", ".avi"}


class MediaError(RuntimeError):
    """Raised when conversion or validation of a media file fails."""


# --------------------------------------------------------------------------- #
# Tool discovery
# --------------------------------------------------------------------------- #
def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise MediaError("ffmpeg not found on PATH (required for video emoji).")
    return exe


def ffprobe_path() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise MediaError("ffprobe not found on PATH (required for video emoji).")
    return exe


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=capture)


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #
def detect_format_bytes(head: bytes) -> str:
    """Return 'static' | 'animated' | 'video' | 'unknown' from leading bytes."""
    if head.startswith(_EBML_MAGIC):
        return "video"                       # .webm
    if head.startswith(_GZIP_MAGIC):
        return "animated"                    # .tgs (gzip-compressed Lottie)
    if head.startswith(_PNG_MAGIC) or head.startswith(_GIF_MAGIC):
        return "static"
    if head.startswith(_RIFF_MAGIC) and head[8:12] == b"WEBP":
        return "static"                      # static .webp sticker
    return "unknown"


def detect_format(path: Path) -> str:
    """Detect the Telegram emoji format of a file from its content."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    fmt = detect_format_bytes(head)
    if fmt != "unknown":
        return fmt
    # Fall back to extension hints for ambiguous local sources.
    ext = path.suffix.lower()
    if ext == ".tgs":
        return "animated"
    if ext in {".webm", ".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
        return "video"
    if ext in RASTER_EXTS or ext == ".svg":
        return "static"
    return "unknown"


def telegram_sticker_format(sticker: dict) -> str:
    """Map a Bot API Sticker object to an emoji format using is_animated/is_video."""
    if sticker.get("is_animated"):
        return "animated"
    if sticker.get("is_video"):
        return "video"
    return "static"


def ext_for_format(fmt: str) -> str:
    return {"static": ".png", "animated": ".tgs", "video": ".webm"}.get(fmt, ".bin")


def media_extension(path: Path, fmt: str) -> str:
    """Real file extension for a media file, refining static into PNG vs WEBP.

    Telegram static stickers are usually WEBP; using the correct extension keeps
    the upload MIME type consistent with the actual bytes.
    """
    if fmt == "video":
        return ".webm"
    if fmt == "animated":
        return ".tgs"
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(_PNG_MAGIC):
        return ".png"
    if head.startswith(_RIFF_MAGIC) and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(_GIF_MAGIC):
        return ".gif"
    return ".png"


# --------------------------------------------------------------------------- #
# Static image fitting (shared with the legacy make_emoji_pngs pipeline)
# --------------------------------------------------------------------------- #
def _trim(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    bbox = img.split()[3].getbbox()
    return img.crop(bbox) if bbox else img


def fit_100(img: Image.Image) -> Image.Image:
    """Trim transparent borders and center the image on a 100x100 RGBA canvas."""
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


def _load_image(src: Path) -> Image.Image:
    """Decode a raster or SVG source into an RGBA Pillow image."""
    if src.suffix.lower() == ".svg":
        # svglib/reportlab are only needed for SVG; import lazily.
        from make_emoji_pngs import _render_svg  # type: ignore
        img = _render_svg(src)
        if img is None:
            raise MediaError(f"failed to render SVG: {src.name}")
        return img
    return Image.open(src).convert("RGBA")


def to_static_png(src: Path, out: Path) -> Path:
    """Convert any supported image into a 100x100 transparent PNG."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fit_100(_load_image(src)).save(out, format="PNG", optimize=True)
    return out


# --------------------------------------------------------------------------- #
# Video (WEBM/VP9) conversion + validation
# --------------------------------------------------------------------------- #
_VF = (
    f"fps={WEBM_FPS},scale=w={SIZE}:h={SIZE}:force_original_aspect_ratio=decrease"
    f":flags=lanczos,format=rgba,pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2"
    f":color=0x00000000,format=yuva420p"
)


def to_video_webm(src: Path, out: Path, *, max_bytes: int = WEBM_MAX_BYTES,
                  loop_still: bool = False, seconds: float = WEBM_MAX_SECONDS) -> Path:
    """Encode any animation/video/image into a Telegram-compliant VP9 WEBM emoji.

    Output is 100x100, <=3 s, 30 fps, no audio, transparent-padded, VP9 with
    alpha. CRF is escalated until the file fits ``max_bytes``.

    Set ``loop_still=True`` when the source is a still image (e.g. a logo PNG):
    the image is looped for ``seconds`` so the result is a valid, non-zero
    duration video emoji rather than a single zero-length frame.
    """
    ff = ffmpeg_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    last_size = -1
    for crf in (32, 40, 48, 56, 63):
        pre = ["-loop", "1"] if loop_still else []
        cmd = [
            ff, "-y", *pre, "-t", str(seconds), "-i", str(src),
            "-an", "-vf", _VF,
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", "0", "-crf", str(crf),
            "-auto-alt-ref", "0", "-deadline", "good", "-cpu-used", "2",
            str(out),
        ]
        _run(cmd, capture=True)
        last_size = out.stat().st_size
        log.debug("webm crf=%d -> %d bytes", crf, last_size)
        if last_size <= max_bytes:
            return out
    raise MediaError(
        f"could not fit {src.name} under {max_bytes} bytes (got {last_size}).")


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    codec: str


def probe_video(path: Path) -> VideoInfo:
    """Read width/height/duration/codec of a video file via ffprobe."""
    ff = ffprobe_path()
    cmd = [ff, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name:format=duration",
           "-of", "json", str(path)]
    res = _run(cmd, capture=True)
    data = json.loads(res.stdout.decode("utf-8", "replace"))
    stream = (data.get("streams") or [{}])[0]
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    return VideoInfo(int(stream.get("width", 0)), int(stream.get("height", 0)),
                     dur, str(stream.get("codec_name", "")))


def validate_video(path: Path) -> None:
    """Raise MediaError if a WEBM does not meet Telegram video-emoji rules."""
    info = probe_video(path)
    size = path.stat().st_size
    problems = []
    if (info.width, info.height) != (SIZE, SIZE):
        problems.append(f"dimensions {info.width}x{info.height} != {SIZE}x{SIZE}")
    if info.duration > WEBM_MAX_SECONDS + 0.05:
        problems.append(f"duration {info.duration:.2f}s > {WEBM_MAX_SECONDS}s")
    if info.codec != "vp9":
        problems.append(f"codec {info.codec!r} != 'vp9'")
    if size > WEBM_MAX_BYTES:
        problems.append(f"size {size} > {WEBM_MAX_BYTES} bytes")
    if problems:
        raise MediaError(f"{path.name}: " + "; ".join(problems))


# --------------------------------------------------------------------------- #
# Animated (TGS) packaging + validation
# --------------------------------------------------------------------------- #
def _load_lottie(src: Path) -> dict:
    """Load a Lottie animation from .json or .tgs into a dict."""
    raw = src.read_bytes()
    if raw[:2] == _GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def to_animated_tgs(src: Path, out: Path) -> Path:
    """Package a Lottie animation (.json or .tgs) into a valid 100x100 .tgs.

    NOTE: animated emoji are VECTOR (Lottie) only. Raster sources (GIF/MP4/WEBM)
    CANNOT become animated emoji -- convert those to *video* emoji instead. This
    function only validates/repackages an existing Lottie animation.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    lottie = _load_lottie(src)
    # Telegram emoji are 100x100. If the Lottie was authored at another canvas
    # size, set the canvas to 100x100 (a uniform scale of the root is appended
    # so the artwork is not clipped).
    w, h = int(lottie.get("w", 0)), int(lottie.get("h", 0))
    if (w, h) != (SIZE, SIZE) and w and h:
        log.warning("Lottie canvas %dx%d != %dx%d; rescaling to fit.", w, h, SIZE, SIZE)
        _rescale_lottie(lottie, w, h)
        lottie["w"] = SIZE
        lottie["h"] = SIZE
    data = json.dumps(lottie, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # mtime=0 keeps the gzip output deterministic for stable content hashing.
    with open(out, "wb") as fh:
        with gzip.GzipFile(filename="", fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(data)
    validate_tgs(out)
    return out


def _rescale_lottie(lottie: dict, w: int, h: int) -> None:
    """Wrap layers in a scaling transform so a non-100x100 Lottie fits 100x100."""
    scale = min(SIZE / w, SIZE / h) * 100.0  # Lottie scale is in percent
    for layer in lottie.get("layers", []):
        ks = layer.setdefault("ks", {})
        s = ks.get("s", {"a": 0, "k": [100, 100, 100]})
        k = s.get("k", [100, 100, 100])
        if isinstance(k, list) and len(k) >= 2 and all(isinstance(v, (int, float)) for v in k[:2]):
            s["k"] = [k[0] * scale / 100.0, k[1] * scale / 100.0,
                      k[2] if len(k) > 2 else 100]
            s["a"] = 0
            ks["s"] = s


def validate_tgs(path: Path) -> None:
    """Raise MediaError if a .tgs is invalid or exceeds Telegram's size cap."""
    size = path.stat().st_size
    if size > TGS_MAX_BYTES:
        raise MediaError(f"{path.name}: TGS {size} > {TGS_MAX_BYTES} bytes")
    try:
        lottie = _load_lottie(path)
    except (OSError, ValueError) as exc:
        raise MediaError(f"{path.name}: not a valid TGS/Lottie ({exc})") from exc
    for key in ("v", "fr", "ip", "op", "layers"):
        if key not in lottie:
            raise MediaError(f"{path.name}: Lottie missing required key {key!r}")


# --------------------------------------------------------------------------- #
# Deduplication keys
# --------------------------------------------------------------------------- #
def _norm_pixels(img: Image.Image, n: int = 64) -> bytes:
    return img.convert("RGBA").resize((n, n), Image.LANCZOS).tobytes()


def content_key(path: Path, fmt: str) -> str:
    """Return a strong, normalized content hash used as the dedup primary key.

    Two files that render to the same emoji collapse onto the same key, even if
    their container bytes differ (different compression, re-export, etc.).
    """
    if fmt == "static":
        digest = hashlib.sha256(_norm_pixels(Image.open(path))).hexdigest()
        return "s:" + digest[:32]
    if fmt == "video":
        return "v:" + _video_content_digest(path)[:32]
    if fmt == "animated":
        return "a:" + _animated_content_digest(path)[:32]
    # Unknown format: fall back to raw bytes so it is at least exactly deduped.
    return "r:" + hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def _video_content_digest(path: Path) -> str:
    """Hash normalized sampled frames so visually identical videos match."""
    ff = ffmpeg_path()
    cmd = [ff, "-v", "error", "-t", str(WEBM_MAX_SECONDS), "-i", str(path),
           "-an", "-vf", "fps=10,scale=64:64,format=rgba", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, check=True, capture_output=True)
    if res.stdout:
        return hashlib.sha256(res.stdout).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _animated_content_digest(path: Path) -> str:
    """Hash the canonicalized Lottie JSON so re-gzipped TGS files match."""
    try:
        lottie = _load_lottie(path)
        canon = json.dumps(lottie, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canon).hexdigest()
    except (OSError, ValueError):
        return hashlib.sha256(path.read_bytes()).hexdigest()


def perceptual_hash(path: Path, fmt: str) -> int | None:
    """64-bit difference hash (dHash) for near-duplicate detection.

    Defined for raster formats (static, and the first frame of a video). Returns
    None for animated TGS (vector; no cheap raster to hash).
    """
    try:
        if fmt == "static":
            img = Image.open(path)
        elif fmt == "video":
            img = _first_video_frame(path)
        else:
            return None
    except Exception as exc:  # noqa: BLE001
        log.debug("phash failed for %s: %s", path.name, exc)
        return None
    return _dhash(img)


def _first_video_frame(path: Path) -> Image.Image:
    ff = ffmpeg_path()
    cmd = [ff, "-v", "error", "-i", str(path), "-frames:v", "1",
           "-vf", "scale=64:64,format=rgba", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, check=True, capture_output=True)
    return Image.frombytes("RGBA", (64, 64), res.stdout[: 64 * 64 * 4])


def _dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Difference hash: compare adjacent pixels of a (hash_size+1) x hash_size gray image."""
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = gray.tobytes()  # one byte per pixel for mode "L"
    row_stride = hash_size + 1
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[row * row_stride + col]
            right = px[row * row_stride + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """Hamming distance between two perceptual hashes."""
    return bin(a ^ b).count("1")
