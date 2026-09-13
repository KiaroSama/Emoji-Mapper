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
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("emojikit.video")

# The sample geometry both identity keys are built from. 64x64 RGBA, one frame.
FRAME_SIDE = 64
FRAME_BYTES = FRAME_SIDE * FRAME_SIDE * 4

# Sampling at a fixed rate is what makes two encodes of one clip agree.
SAMPLE_FPS = 10

# Which ffmpeg decoder actually understands each codec's alpha. The native
# decoders for these two drop the alpha layer silently; the libvpx ones do not.
_ALPHA_CAPABLE = {"vp9": "libvpx-vp9", "vp8": "libvpx"}

# (path, size, mtime_ns) -> decoder args. Keyed on the file's identity rather
# than its name because `reencode_in_place` rewrites files where they stand,
# and a stale decoder choice would outlive the bytes it was chosen for.
_decoder_cache: dict[tuple[str, int, int], tuple[str, ...]] = {}
_available: dict[str, bool] = {}

# Decoding is the expensive step, and one question about a pair of videos asks
# for it many times over: `same_image` alone wants two content keys, two
# perceptual hashes and two rendered frames, which was six ffmpeg runs over two
# files. Keyed on the file's identity rather than its name, exactly like the
# decoder cache, so bytes rewritten in place are never answered from a stale
# entry. Bounded because a sample stream is ~0.5 MB and an ingest walks
# thousands of files: this is a working set, not a store.
_FRAME_CACHE_MAX = 8
_frame_cache: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()


def _media():
    """Imported lazily: `media` imports this module, and a cycle at import time
    would leave one of them half-initialised."""
    from emojikit import media
    return media


def decoder_available(name: str) -> bool:
    """Is this decoder compiled into the ffmpeg on PATH?

    Asked once per name per process. A build without libvpx would otherwise
    fail every single decode, which is a worse outcome than losing alpha -- but
    losing alpha silently is exactly the defect above, so it is logged loudly.
    """
    if name in _available:
        return _available[name]
    media = _media()
    try:
        out = media._run([media.ffmpeg_path(), "-v", "error", "-decoders"],
                         capture=True).stdout or b""
        ok = name.encode() in out
    except Exception as exc:  # noqa: BLE001 - unknown capability, assume absent
        log.debug("could not list ffmpeg decoders: %s", exc)
        ok = False
    if not ok:
        log.warning("ffmpeg has no %s decoder: alpha in VP8/VP9 video cannot be "
                    "read, so two clips differing only in transparency may look "
                    "identical to this run", name)
    _available[name] = ok
    return ok


def decoder_args(path: Path) -> list[str]:
    """ffmpeg input flags that decode ``path`` WITH its alpha, if it has any.

    Empty for anything that is not a codec with a known alpha-dropping default
    decoder, and empty when the right decoder is not available -- in which case
    the caller gets the same answer it used to, and `decoder_available` has
    already said so in the log.
    """
    path = Path(path)
    try:
        st = path.stat()
        cache_key = (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _decoder_cache:
        return list(_decoder_cache[cache_key])

    codec = ""
    try:
        codec = (_media().probe_video(path).codec or "").lower()
    except Exception as exc:  # noqa: BLE001 - unprobeable: let ffmpeg decide
        log.debug("codec probe failed for %s: %s", path.name, exc)

    name = _ALPHA_CAPABLE.get(codec, "")
    args: tuple[str, ...] = ()
    if name and decoder_available(name):
        args = ("-c:v", name)
    if cache_key is not None:
        _decoder_cache[cache_key] = args
    return list(args)


def frames_rgba(path: Path) -> bytes:
    """The canonical RGBA sample stream: 10 fps, 64x64, alpha preserved.

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
        cache_key = (str(path), st.st_size, st.st_mtime_ns)
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

    raw = sample(f"fps={SAMPLE_FPS},") or sample("")
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
