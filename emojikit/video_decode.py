"""Decoding a video the same way every time, with its alpha intact.

THE canonical video sample stream. Everything that asks "what does this video
look like" -- the content key, the perceptual hash, the visual comparison, the
re-encoder's input -- goes through here, so they cannot answer differently.

Two defects made this a module instead of a few lines.

**Alpha was thrown away (F12).** VP9 keeps alpha in a SEPARATE WebM layer and
ffmpeg's native ``vp9`` decoder drops it without a word: two real clips with
identical RGB and alpha 255 versus 129 both decoded to alpha 255, so they
produced ONE content key, ``same_image`` called them equal, and ``Catalog.add``
merged them and deleted the second file. Naming ``libvpx-vp9`` is what carries
transparency through a decode.

**The decoder was chosen by file extension.** ``to_video_webm`` tested
``suffix == ".webm"``, which forces a VP9 decoder onto a VP8 WebM (wrong codec)
and skips it entirely for a download saved without an extension (alpha lost).
The container knows its codec; ask it.

The remaining rule is the one that made two public APIs disagree (F13): there
is ONE sampling filter, and the "first frame" is the first frame OF THAT
STREAM. ``fps=10`` does not necessarily emit native frame zero -- measured: a
30 fps clip whose first frame differs gave 5300177295401782857 through
``fingerprint`` and 6510615555426900570 through ``perceptual_hash`` -- so
"first frame" is only well defined once the stream it belongs to is.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
from collections import OrderedDict
from pathlib import Path

from emojikit.errors import UndecodableVideo

log = logging.getLogger("emojikit.video")

# The sample geometry both identity keys are built from. 64x64 RGBA, one frame.
FRAME_SIDE = 64
FRAME_BYTES = FRAME_SIDE * FRAME_SIDE * 4

# Sampling at a fixed rate is what makes two encodes of one clip agree.
SAMPLE_FPS = 10

# Which ffmpeg decoder actually understands each codec's alpha. The native
# decoders for these two drop the alpha layer silently; the libvpx ones do not.
_ALPHA_CAPABLE = {"vp9": "libvpx-vp9", "vp8": "libvpx"}

# (path, size, mtime_ns, ffmpeg identity) -> decoder args. Keyed on the file's
# identity rather than its name because `reencode_in_place` rewrites files where
# they stand and a stale decoder choice would outlive the bytes it was chosen
# for; keyed on the ffmpeg too because the answer is a fact about that binary.
# Bounded for the same reason as the frame cache below: a working set, not a
# record. Only determinations that passed every check in `decoder_args` land
# here -- a failure is never cached, which is what made one transient probe
# error permanent.
_DECODER_CACHE_MAX = 512
_decoder_cache: OrderedDict[tuple[str, int, int, tuple[str, int, int]],
                            tuple[str, ...]] = OrderedDict()
_available: dict[tuple[str, tuple[str, int, int]], bool] = {}

# Decoding is the expensive step, and one question about a pair of videos asks
# for it many times over: `same_image` alone wants two content keys, two
# perceptual hashes and two rendered frames, which was six ffmpeg runs over two
# files. Keyed on the file's identity rather than its name, exactly like the
# decoder cache, so bytes rewritten in place are never answered from a stale
# entry. Bounded because a sample stream is ~0.5 MB and an ingest walks
# thousands of files: this is a working set, not a store.
_FRAME_CACHE_MAX = 8
_frame_cache: OrderedDict[tuple[str, int, int, int], bytes] = OrderedDict()
_timeline_cache: OrderedDict[tuple, tuple[bytes, tuple[float, ...], float]] = OrderedDict()
MAX_NATIVE_FRAMES = 128


def _media():
    """Imported lazily: `media` imports this module, and a cycle at import time
    would leave one of them half-initialised."""
    from emojikit import media
    return media


def _ffmpeg_identity() -> tuple[str, int, int]:
    """What a capability answer is true OF.

    "This ffmpeg has no libvpx-vp9" is a fact about one binary, and it used to
    be cached as a fact about the process. Keying on the executable itself is
    what lets a rebuilt or swapped toolchain be noticed without a restart. A
    binary that cannot be stat'd keys on its path alone -- weaker, and the
    reason this returns a tuple rather than a bare string.
    """
    media = _media()
    p = Path(media.ffmpeg_path())
    if not p.is_absolute():
        p = Path(shutil.which(str(p)) or p)
    try:
        st = p.stat()
        return (str(p), st.st_size, st.st_mtime_ns)
    except OSError:
        return (str(p), -1, -1)


def decoder_available(name: str) -> bool:
    """Is this decoder compiled into the ffmpeg on PATH?

    Raises rather than answering False when the QUESTION could not be asked.
    "ffmpeg says it has no libvpx-vp9" and "ffmpeg did not run" are different
    facts and only the first is cacheable -- the second one used to be stored as
    False, so a single transient failure made every later video in the process
    decode without its alpha, silently and permanently.
    """
    key = (name, _ffmpeg_identity())
    if key in _available:
        return _available[key]
    media = _media()
    try:
        out = media._run([media.ffmpeg_path(), "-v", "error", "-decoders"],
                         capture=True).stdout or b""
    except Exception as exc:
        raise UndecodableVideo(
            f"could not ask ffmpeg which decoders it has, so alpha fidelity "
            f"cannot be established: {exc}") from exc
    ok = name.encode() in out
    if not ok:
        log.warning("ffmpeg has no %s decoder: alpha in VP8/VP9 video cannot be "
                    "read, so identity for those clips cannot be established "
                    "here", name)
    _available[key] = ok
    return ok


def decoder_args(path: Path) -> list[str]:
    """ffmpeg input flags that decode ``path`` WITH its alpha.

    Raises ``UndecodableVideo`` whenever that cannot be ESTABLISHED, and returns
    ``[]`` only on positive evidence that no override applies -- the file is not
    a video at all, or its container named a codec whose default decoder keeps
    transparency.

    It used to degrade instead: a failed probe, an unnamed codec or a missing
    libvpx all produced ``[]``, which decodes a VP9 clip without its alpha
    layer. Two clips differing only in opacity then share one content key, and
    `Catalog.add` merges them and deletes the file it merged away. Guessing here
    costs media; raising costs one skipped item that gets counted and reported.
    """
    path = Path(path)
    media = _media()

    # Magic bytes, not a probe: an image or a GIF has no alpha-dropping video
    # decoder to choose, so the strictness below is confined to real videos and
    # still-image conversion is untouched.
    if media.detect_format(path) != "video":
        return []

    ffmpeg = _ffmpeg_identity()
    try:
        st = path.stat()
        cache_key = (str(path), st.st_size, st.st_mtime_ns, ffmpeg)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _decoder_cache:
        _decoder_cache.move_to_end(cache_key)
        return list(_decoder_cache[cache_key])

    try:
        codec = (media.probe_video(path).codec or "").lower()
    except Exception as exc:
        raise UndecodableVideo(
            f"{path.name}: the codec could not be read, so the decoder that "
            f"preserves its alpha cannot be chosen: {exc}") from exc
    if not codec:
        # Missing metadata is not evidence of a codec that needs no override.
        raise UndecodableVideo(
            f"{path.name}: the container named no video codec, so alpha "
            f"fidelity cannot be established")

    name = _ALPHA_CAPABLE.get(codec, "")
    if name and not decoder_available(name):
        raise UndecodableVideo(
            f"{path.name}: {codec} keeps alpha in a separate layer and this "
            f"ffmpeg has no {name} decoder, so a decode here cannot be trusted "
            f"to carry transparency")

    args = ("-c:v", name) if name else ()
    if cache_key is not None:
        # Only a determination reached through the checks above is stored, and
        # the store is bounded: an ingest walks thousands of files, and this is
        # a working set rather than a record.
        _decoder_cache[cache_key] = args
        _decoder_cache.move_to_end(cache_key)
        while len(_decoder_cache) > _DECODER_CACHE_MAX:
            _decoder_cache.popitem(last=False)
    return list(args)


def frames_rgba(path: Path, fps: int = SAMPLE_FPS) -> bytes:
    """RGBA frames at ``fps``, 64x64, alpha preserved.

    ``SAMPLE_FPS`` is the IDENTITY stream and its rate is frozen: every stored
    content key is a hash of it, so changing that default re-keys the whole
    catalog. ``fps`` exists for the one caller that needs to look harder --
    comparing two clips, where a change shorter than one sampling interval is
    invisible. Measured: a single differing frame in a 30 fps clip is present at
    30 fps and gone at 10 and at 15.

    Failure is NOT caught. A hung or timed-out ffmpeg means "I could not look",
    and turning that into an empty sample would turn it into a byte hash -- a
    key that looks fine, never dedups, and hides the timeout. Let MediaError
    out; every ingest site already fails that one item and counts it.

    An EMPTY result from a SUCCESSFUL run is different: the file decoded, it
    just produced no frames at this sampling rate. A single-frame video is
    shorter than one sampling interval and yields nothing, so it is retried at
    the file's own frames before any caller gives up on a content key.
    """
    path = Path(path)
    try:
        st = path.stat()
        cache_key = (str(path), st.st_size, st.st_mtime_ns, fps)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _frame_cache:
        _frame_cache.move_to_end(cache_key)
        return _frame_cache[cache_key]

    media = _media()
    ff = media.ffmpeg_path()
    decoder = decoder_args(path)

    def sample(rate_filter: str) -> bytes:
        cmd = [ff, "-v", "error", "-t", str(media.WEBM_MAX_SECONDS), *decoder,
               "-i", str(path), "-an",
               "-vf", f"{rate_filter}scale={FRAME_SIDE}:{FRAME_SIDE},format=rgba",
               "-f", "rawvideo", "-"]
        return media._run(cmd, capture=True).stdout or b""

    raw = sample(f"fps={fps},") or sample("")
    # Only a successful decode is cached. An empty result from a file that
    # genuinely renders nothing is cheap to repeat, and a FAILED decode raises
    # before reaching here -- caching "I could not look" would turn one
    # transient timeout into a permanent answer.
    if cache_key is not None and raw:
        _frame_cache[cache_key] = raw
        _frame_cache.move_to_end(cache_key)
        while len(_frame_cache) > _FRAME_CACHE_MAX:
            _frame_cache.popitem(last=False)
    return raw


def first_frame_bytes(raw: bytes) -> bytes | None:
    """Frame zero OF THE CANONICAL STREAM, or None if there is not a whole one.

    Deliberately a slice of the same bytes every other answer is built from.
    The previous "first frame" ran its own ffmpeg with no fps filter, which is
    a DIFFERENT frame -- that is F13 in one line.
    """
    return raw[:FRAME_BYTES] if len(raw) >= FRAME_BYTES else None


def timeline_rgba(path: Path) -> tuple[bytes, tuple[float, ...], float]:
    """Every native frame and its presentation time, without dropping VFR frames.

    The frozen 10 fps stream is a lookup hint. Even 30 fps misses brief VFR
    frames, so destructive identity decisions need the actual decoded frames.
    Unsupported lengths and incomplete timing evidence raise, never truncate.
    """
    path = Path(path)
    st = path.stat()
    cache_key = (str(path.resolve()), st.st_size, st.st_mtime_ns, _ffmpeg_identity())
    if cache_key in _timeline_cache:
        _timeline_cache.move_to_end(cache_key)
        return _timeline_cache[cache_key]
    media = _media()
    info = media.probe_video(path)
    if not math.isfinite(info.duration) or not 0 < info.duration <= media.WEBM_MAX_SECONDS + 0.05:
        raise UndecodableVideo(f"{path.name}: complete video duration is outside the comparison limit")
    result = media._run([
        media.ffmpeg_path(), "-v", "info", *decoder_args(path), "-i", str(path),
        "-map", "0:v:0", "-an", "-vf",
        f"scale={FRAME_SIDE}:{FRAME_SIDE},format=rgba,showinfo",
        "-fps_mode", "passthrough", "-frames:v", str(MAX_NATIVE_FRAMES + 1),
        "-f", "rawvideo", "-"], capture=True)
    raw = result.stdout or b""
    times = tuple(float(t) for t in re.findall(
        rb"\bn:\s*\d+\s+pts:\s*\S+\s+pts_time:([^\s]+)", result.stderr or b""))
    count, remainder = divmod(len(raw), FRAME_BYTES)
    if remainder or not 0 < count <= MAX_NATIVE_FRAMES or len(times) != count:
        raise UndecodableVideo(f"{path.name}: native frames and timestamps are incomplete")
    if not all(math.isfinite(t) for t in times) or any(
            b <= a for a, b in zip(times, times[1:], strict=False)):
        raise UndecodableVideo(f"{path.name}: native frame timestamps cannot be aligned")
    origin = times[0]
    duration = info.duration - origin
    normalized = tuple(t - origin for t in times)
    if not 0 < duration - normalized[-1] <= media.WEBM_MAX_SECONDS:
        raise UndecodableVideo(f"{path.name}: final frame duration cannot be established")
    value = (raw, normalized, duration)
    _timeline_cache[cache_key] = value
    while len(_timeline_cache) > _FRAME_CACHE_MAX:
        _timeline_cache.popitem(last=False)
    return value
