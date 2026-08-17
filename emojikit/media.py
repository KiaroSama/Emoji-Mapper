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

Raster/vector image decoding uses Pillow (+ resvg for SVG). Video and
animated-video work requires ``ffmpeg``/``ffprobe`` on PATH; every such child
runs under a wall-clock limit (``EMOJI_FFMPEG_TIMEOUT``, default 300 s) so one
corrupt file cannot stall an ingest or publish run.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger("emojikit.media")

SIZE = 100                       # static/video custom emoji canvas (px)
# Animated emoji are the exception: Telegram requires a 512x512 Lottie canvas
# for .tgs, the same as animated stickers -- only the static and video sections
# of core.telegram.org/stickers narrow the canvas to 100x100 for emoji.
TGS_SIZE = 512
TGS_MAX_SECONDS = 3.0
TGS_FPS = 60
TGS_MAX_UNPACKED = 8 * 1024 * 1024   # bound decompression of a hostile .tgs
TGS_MAX_BYTES = 64 * 1024        # animated emoji hard cap
WEBM_MAX_BYTES = 256 * 1024      # video emoji hard cap
WEBM_MAX_SECONDS = 3.0
WEBM_FPS = 30

# Every ffmpeg/ffprobe child runs under a wall limit. A truncated container or a
# wedged decoder makes the tool wait on its input forever, and an unbounded child
# stalls the whole ingest/publish run with no output and no error.
FFMPEG_TIMEOUT = 300             # seconds per child; a 3 s emoji encode is <1 s
_KILL_GRACE = 5                  # seconds allowed to kill and reap a stuck child

# Shared "is there anything to see?" rule, also used by the publisher: alpha at
# or below VISIBLE_ALPHA is invisible in practice, and a handful of stray pixels
# is noise, not artwork.
VISIBLE_ALPHA = 10
BLANK_MAX_VISIBLE = 8

# Container/codec magic bytes used for fast format sniffing.
_GZIP_MAGIC = b"\x1f\x8b"
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"      # Matroska/WebM
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".apng"}


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


def ff_timeout() -> float:
    """Per-child ffmpeg/ffprobe wall limit; override with EMOJI_FFMPEG_TIMEOUT."""
    # Lazy import: emojikit stays importable without the CLI layer (and this is
    # called once per child process, so the sys.modules lookup is free).
    from build_pack import safe_int_env
    return safe_int_env("EMOJI_FFMPEG_TIMEOUT", FFMPEG_TIMEOUT, minimum=1)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a stuck child *and its descendants*, then reap it.

    Killing only the direct child can leave a grandchild holding the output
    pipes open, so the follow-up read blocks for exactly as long as the hang we
    are trying to bound.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False, timeout=_KILL_GRACE)
        else:
            # start_new_session below makes the child its own group leader, so
            # this kills its whole tree without touching our own process group.
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass                              # already gone, or not ours to signal
    try:
        proc.kill()
        proc.communicate(timeout=_KILL_GRACE)
    except (OSError, subprocess.SubprocessError):
        pass


def _run(cmd: list[str], *, capture: bool = False,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one ffmpeg/ffprobe child under a finite wall limit.

    A hang is reported as :class:`MediaError` like any other conversion failure,
    so a single corrupt file is skipped instead of freezing the run. Non-zero
    exits keep raising ``CalledProcessError`` as before.
    """
    limit = ff_timeout() if timeout is None else timeout
    log.debug("exec (timeout %ss): %s", limit, " ".join(cmd))
    pipe = subprocess.PIPE if capture else None
    # POSIX: own session so _kill_tree can signal the group. Windows uses
    # taskkill /T instead, which needs no creation flag (and setting one would
    # stop Ctrl+C from reaching the child).
    extra = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, **extra)
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        raise MediaError(
            f"{Path(cmd[0]).name} timed out after {limit}s") from exc
    except BaseException:                 # Ctrl+C must not orphan the child
        _kill_tree(proc)
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
        # The SVG rasterizer is only needed for SVG; import lazily.
        from make_emoji_pngs import _render_svg  # type: ignore
        img = _render_svg(src)
        if img is None:
            raise MediaError(f"failed to render SVG: {src.name}")
        return img
    return Image.open(src).convert("RGBA")


def is_blank_image(img: Image.Image) -> bool:
    """True if an image has no meaningful visible pixels.

    Shared rule so every producer and the publisher agree on what "blank" means.
    """
    alpha = img.convert("RGBA").split()[3]
    if alpha.getbbox() is None:
        return True
    # histogram() counts in C; the per-pixel generator this replaced walked
    # 10 000 Python iterations per image, and this runs on every conversion AND
    # on every static item of every publish. Bucket i holds the number of pixels
    # with alpha == i, so summing from VISIBLE_ALPHA + 1 upwards is exactly
    # "how many pixels are more opaque than the visibility floor".
    return sum(alpha.histogram()[VISIBLE_ALPHA + 1:]) <= BLANK_MAX_VISIBLE


def to_static_png(src: Path, out: Path) -> Path:
    """Convert any supported image into a 100x100 transparent PNG.

    Raises MediaError if the result would be blank: a transparent emoji is
    invisible forever, and ingesting one pollutes the catalog with an item that
    can never be used but still occupies one of the 200 slots in a pack.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    img = fit_100(_load_image(src))
    if is_blank_image(img):
        raise MediaError(f"{src.name}: image is blank (no visible pixels)")
    img.save(out, format="PNG", optimize=True)
    return out


# --------------------------------------------------------------------------- #
# Video (WEBM/VP9) conversion + validation
# --------------------------------------------------------------------------- #
_VF = (
    f"fps={WEBM_FPS},scale=w={SIZE}:h={SIZE}:force_original_aspect_ratio=decrease"
    f":flags=lanczos,format=rgba,pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2"
    f":color=0x00000000,format=yuva420p"
)


def to_video_webm(src: Path, out: Path, *, max_bytes: int = WEBM_MAX_BYTES) -> Path:
    """Encode any animation/video/image into a Telegram-compliant VP9 WEBM emoji.

    Output is 100x100, <=3 s, 30 fps, no audio, transparent-padded, VP9 with
    alpha. CRF is escalated until the file fits ``max_bytes``.
    """
    ff = ffmpeg_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    last_size = -1
    for crf in (32, 40, 48, 56, 63):
        cmd = [
            ff, "-y", "-t", str(WEBM_MAX_SECONDS), "-i", str(src),
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
    fps: float = 0.0
    has_audio: bool = False
    container: str = ""


def _fps(rate: str) -> float:
    """Parse an ffprobe rational frame rate ('30/1', '60000/1001')."""
    try:
        num, _, den = rate.partition("/")
        return float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: Path) -> VideoInfo:
    """Read the properties Telegram actually constrains, via ffprobe.

    Selecting only ``v:0`` hides the audio stream, so an .webm carrying audio
    used to validate cleanly; frame rate and container were never read at all.
    """
    ff = ffprobe_path()
    cmd = [ff, "-v", "error",
           "-show_entries",
           "stream=index,codec_type,codec_name,width,height,avg_frame_rate"
           ":format=duration,format_name",
           "-of", "json", str(path)]
    res = _run(cmd, capture=True)
    data = json.loads(res.stdout.decode("utf-8", "replace"))
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = data.get("format", {})
    return VideoInfo(
        width=int(video.get("width", 0) or 0),
        height=int(video.get("height", 0) or 0),
        duration=float(fmt.get("duration", 0) or 0),
        codec=str(video.get("codec_name", "")),
        fps=_fps(str(video.get("avg_frame_rate", "0/1"))),
        has_audio=has_audio,
        container=str(fmt.get("format_name", "")),
    )


def validate_video(path: Path) -> None:
    """Raise MediaError if a WEBM does not meet Telegram video-emoji rules.

    Contract: .WEBM container, VP9, no audio stream, exactly 100x100, positive
    duration <= 3 s, <= 30 fps, <= 256 KB. See core.telegram.org/stickers.
    """
    info = probe_video(path)
    size = path.stat().st_size
    problems = []
    if (info.width, info.height) != (SIZE, SIZE):
        problems.append(f"dimensions {info.width}x{info.height} != {SIZE}x{SIZE}")
    if info.duration <= 0:
        problems.append("duration is zero or unknown")
    elif info.duration > WEBM_MAX_SECONDS + 0.05:
        problems.append(f"duration {info.duration:.2f}s > {WEBM_MAX_SECONDS}s")
    if info.codec != "vp9":
        problems.append(f"codec {info.codec!r} != 'vp9'")
    if info.has_audio:
        problems.append("contains an audio stream (video emoji must have none)")
    if info.fps > WEBM_FPS + 0.01:
        problems.append(f"{info.fps:.2f} fps > {WEBM_FPS} fps")
    if "webm" not in info.container.split(","):
        problems.append(f"container {info.container!r} is not webm")
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
        # Bounded: a few KB of gzip can expand to gigabytes, and the compressed
        # size check happens after this.
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            raw = gz.read(TGS_MAX_UNPACKED + 1)
        if len(raw) > TGS_MAX_UNPACKED:
            raise MediaError(f"{src.name}: Lottie expands beyond "
                             f"{TGS_MAX_UNPACKED} bytes")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        # A list/scalar root would otherwise surface as AttributeError far away
        # from here.
        raise MediaError(f"{src.name}: Lottie root is {type(data).__name__}, "
                         f"expected an object")
    return data


def to_animated_tgs(src: Path, out: Path) -> Path:
    """Package a Lottie animation (.json or .tgs) into a valid 512x512 .tgs.

    NOTE: animated emoji are VECTOR (Lottie) only. Raster sources (GIF/MP4/WEBM)
    CANNOT become animated emoji -- convert those to *video* emoji instead. This
    function only validates/repackages an existing Lottie animation.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    lottie = _load_lottie(src)
    w, h = int(lottie.get("w", 0)), int(lottie.get("h", 0))
    if (w, h) != (TGS_SIZE, TGS_SIZE):
        # Rewriting the canvas is not a safe repair: a Lottie's positions,
        # anchors, animated transforms, masks and nested precompositions are all
        # expressed in canvas units, so scaling only the top-level layer
        # transform moves and clips the artwork. Reject instead of shipping a
        # broken animation.
        raise MediaError(
            f"{src.name}: animated emoji require a {TGS_SIZE}x{TGS_SIZE} Lottie "
            f"canvas, got {w}x{h}. Re-export the animation at "
            f"{TGS_SIZE}x{TGS_SIZE}.")
    data = json.dumps(lottie, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # mtime=0 keeps the gzip output deterministic for stable content hashing.
    with open(out, "wb") as fh:
        with gzip.GzipFile(filename="", fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(data)
    validate_tgs(out)
    return out


def validate_tgs(path: Path) -> None:
    """Raise MediaError unless a .tgs satisfies Telegram's animated contract.

    Checks the whole published contract, not just the byte cap: a .tgs is a
    GZIP package, the canvas must be 512x512, and the timeline must run at
    60 fps for at most 3 seconds. See core.telegram.org/stickers.
    """
    size = path.stat().st_size
    if size > TGS_MAX_BYTES:
        raise MediaError(f"{path.name}: TGS {size} > {TGS_MAX_BYTES} bytes")
    if path.read_bytes()[:2] != _GZIP_MAGIC:
        raise MediaError(f"{path.name}: TGS must be gzip-compressed Lottie")
    try:
        lottie = _load_lottie(path)
    except (OSError, ValueError) as exc:
        raise MediaError(f"{path.name}: not a valid TGS/Lottie ({exc})") from exc
    for key in ("v", "fr", "ip", "op", "layers"):
        if key not in lottie:
            raise MediaError(f"{path.name}: Lottie missing required key {key!r}")

    w, h = lottie.get("w"), lottie.get("h")
    if (w, h) != (TGS_SIZE, TGS_SIZE):
        raise MediaError(f"{path.name}: canvas {w}x{h}, expected "
                         f"{TGS_SIZE}x{TGS_SIZE}")
    try:
        fr = float(lottie["fr"])
        frames = float(lottie["op"]) - float(lottie["ip"])
    except (TypeError, ValueError) as exc:
        raise MediaError(f"{path.name}: non-numeric fr/ip/op ({exc})") from exc
    if fr <= 0:
        raise MediaError(f"{path.name}: frame rate {fr} must be positive")
    if fr > TGS_FPS:
        raise MediaError(f"{path.name}: {fr} fps > {TGS_FPS} fps")
    duration = frames / fr
    if duration <= 0:
        raise MediaError(f"{path.name}: empty timeline (ip={lottie['ip']}, "
                         f"op={lottie['op']})")
    if duration > TGS_MAX_SECONDS + 0.01:
        raise MediaError(f"{path.name}: {duration:.2f}s > {TGS_MAX_SECONDS}s")


# --------------------------------------------------------------------------- #
# Deduplication keys
# --------------------------------------------------------------------------- #
def reencode_in_place(path: Path, fmt: str) -> bool:
    """Rewrite ``path`` so its BYTES differ but its picture does not.

    Media pulled from someone else's pack used to be stored and re-uploaded
    byte-for-byte, so the sticker we published was a bit-identical clone of
    theirs. Owner rule: republish our own encoding of the same picture.

    Every branch is pixel-exact, never a lossy re-compress:

    * static  -- decode and re-save WEBP **lossless**. The decoded pixels are
      whatever the source decoded to, lossy or not; encoding them losslessly
      cannot move them.
    * animated -- a .tgs is gzipped Lottie JSON. Re-serialise and re-gzip: the
      animation is the JSON, and the JSON is unchanged.
    * video   -- remux with ``-c copy``. The encoded stream is copied through
      untouched; only the container framing is rewritten.

    Returns True when the file was rewritten. Failure is not fatal to the
    caller: a sticker we could not re-encode is still better ingested as-is
    than dropped, so this reports rather than raises -- but the caller must
    compute the content key AFTERWARDS either way, since the bytes moved.
    """
    before = path.read_bytes()
    try:
        if fmt == "static":
            with Image.open(io.BytesIO(before)) as im:
                rgba = im.convert("RGBA")
            buf = io.BytesIO()
            # exact=True or libwebp rewrites the RGB under fully transparent
            # pixels to compress better. "Lossless" only promises the VISIBLE
            # result; without this the file round-trips to different pixel
            # values, which a real .webp sticker showed and a synthetic
            # fully-opaque fixture never would.
            rgba.save(buf, format="WEBP", lossless=True, quality=100,
                      method=6, exact=True)
            out = buf.getvalue()
        elif fmt == "animated":
            data = _load_lottie(path)
            raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
            buf = io.BytesIO()
            # mtime=0 so the same animation always re-gzips to the same bytes:
            # a timestamp in the header would make ingest non-deterministic and
            # every re-run would look like a different file.
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
                gz.write(raw)
            out = buf.getvalue()
        elif fmt == "video":
            with tempfile.TemporaryDirectory() as td:
                dst = Path(td) / "remux.webm"
                _run([ffmpeg_path(), "-v", "error", "-y", "-i", str(path),
                      "-c", "copy", str(dst)])
                out = dst.read_bytes()
        else:
            return False
    except Exception as exc:  # noqa: BLE001 - ingest must survive one bad file
        log.warning("re-encode skipped for %s (%s): %s", path.name, fmt, exc)
        return False

    if out == before:
        # Nothing gained, and rewriting would only churn the file.
        return False
    # Lossless can GROW a file, and Telegram's per-format caps are hard: a .tgs
    # measured 64 139 bytes after re-encoding against a 65 536 cap, so a source
    # already near the limit can cross it. Publishing a byte-clone is a lesser
    # failure than an upload Telegram rejects, so the original wins here.
    cap = {"animated": TGS_MAX_BYTES, "video": WEBM_MAX_BYTES}.get(fmt)
    if cap is not None and len(out) > cap:
        log.warning("re-encode of %s would be %d bytes, over the %s cap of %d; "
                    "keeping the original", path.name, len(out), fmt, cap)
        return False
    path.write_bytes(out)
    return True


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
    res = _run(cmd, capture=True)
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


def fingerprint(path: Path, fmt: str) -> tuple[str, int | None]:
    """Both identity keys from ONE decode: (content_key, perceptual_hash).

    Every ingest site needs both, and computing them separately decoded the same
    file twice -- for video that meant launching ffmpeg twice over the same clip,
    the most expensive step in the whole ingest path, doubled.

    The video case is where the saving lives: ``_video_content_digest`` already
    renders the clip to ``fps=10,scale=64:64,format=rgba``, and the first frame
    of that stream is byte-identical to what ``_first_video_frame`` fetches on
    its own. So hash the whole stream for the content key and slice frame 0 off
    the front for the dHash. Static decodes once and derives both from the same
    open image; animated has no raster hash, so nothing changes there.

    Values are identical to calling the two functions separately -- same source
    pixels, same filter chain.
    """
    if fmt == "video":
        ff = ffmpeg_path()
        cmd = [ff, "-v", "error", "-t", str(WEBM_MAX_SECONDS), "-i", str(path),
               "-an", "-vf", "fps=10,scale=64:64,format=rgba",
               "-f", "rawvideo", "-"]
        try:
            res = _run(cmd, capture=True)
            raw = res.stdout or b""
        except Exception as exc:  # noqa: BLE001 - fall back to the byte hash
            log.debug("video fingerprint failed for %s: %s", path.name, exc)
            raw = b""
        if not raw:
            return "v:" + hashlib.sha256(path.read_bytes()).hexdigest()[:32], None
        key = "v:" + hashlib.sha256(raw).hexdigest()[:32]
        frame = _VIDEO_FRAME_BYTES
        phash = None
        if len(raw) >= frame:
            try:
                phash = _dhash(Image.frombytes("RGBA", (64, 64), raw[:frame]))
            except Exception as exc:  # noqa: BLE001 - a bad frame is not fatal
                log.debug("phash failed for %s: %s", path.name, exc)
        return key, phash

    if fmt == "static":
        try:
            img = Image.open(path)
            key = "s:" + hashlib.sha256(_norm_pixels(img)).hexdigest()[:32]
        except Exception as exc:  # noqa: BLE001 - unreadable: byte-hash it
            log.debug("static fingerprint failed for %s: %s", path.name, exc)
            return content_key(path, fmt), None
        try:
            return key, _dhash(img)
        except Exception as exc:  # noqa: BLE001
            log.debug("phash failed for %s: %s", path.name, exc)
            return key, None

    return content_key(path, fmt), perceptual_hash(path, fmt)


# One 64x64 RGBA frame, the unit both the content digest and the dHash consume.
_VIDEO_FRAME_BYTES = 64 * 64 * 4


def _first_video_frame(path: Path) -> Image.Image:
    ff = ffmpeg_path()
    cmd = [ff, "-v", "error", "-i", str(path), "-frames:v", "1",
           "-vf", "scale=64:64,format=rgba", "-f", "rawvideo", "-"]
    res = _run(cmd, capture=True)
    return Image.frombytes("RGBA", (64, 64), res.stdout[:_VIDEO_FRAME_BYTES])


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
    """Hamming distance between two perceptual hashes.

    ``int.bit_count()`` rather than ``bin(x).count("1")``: the string form
    allocated a str per comparison, and the panel's similarity ordering calls
    this O(n^2) times -- millions of comparisons on a large catalog.
    """
    return (a ^ b).bit_count()
