"""Identifying a media file: content keys, perceptual hashes, comparison.

Split out of :mod:`emojikit.media`, which makes and converts files; this one
only describes them. The dependency runs one way -- identity needs ffmpeg and
the Lottie loader, conversion never needs a hash -- so importing media here is
safe and importing identity from media would not be.

* :func:`content_key` -- the catalog's primary key. An exact digest of
  normalised pixels/frames, so two encodings of one picture collapse to one row.
* :func:`perceptual_hash` -- a dHash, for near-duplicate search.
* :func:`same_image` -- did OUR file produce THAT sticker? Returns None when it
  cannot be decided; unknown is not false.
* :func:`fingerprint` -- both keys in one pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

# The MODULE, not its names: `from ... import _run` binds once at import
# time, so a test patching media._run to count ffmpeg launches would no
# longer be seen from here -- and counting those launches is exactly what
# fingerprint()'s one-pass guarantee is pinned by.
from emojikit import media

log = logging.getLogger("emojikit.identity")


def _norm_pixels(img: Image.Image, n: int = 64) -> bytes:
    return img.convert("RGBA").resize((n, n), Image.LANCZOS).tobytes()


# Two tolerances, and BOTH must hold. Neither is sufficient alone, which is the
# whole point of this pair:
#
#   * dHash survives Telegram's re-encode but is a GRAYSCALE structural hash,
#     so it is colour-blind. Our red square and a stranger's green square of
#     the same shape sit 4 bits apart -- inside any useful tolerance. Trusting
#     it alone marks our item done against someone else's sticker.
#   * The mean channel delta is colour-aware but says nothing about structure.
#
# Measured over 30 known-same pairs (a local file against the live sticker it
# produced) and 30 known-different pairs, across three published packs:
#
#     dHash bits        same 0..3       different 12..47
#     mean delta        same 0.02..2.35 different 35.46..188.22
#
# 6 bits clears every same pair by 3 and every different one by 6; 8.0 sits
# ~3x above the worst same and ~4x below the closest different. The colour test
# also catches the same-shape-different-colour case dHash cannot see: that pair
# measures 33.44.
UPLOAD_PHASH_TOLERANCE = 6
UPLOAD_MEAN_DELTA = 8.0


def _premultiplied(path: Path, n: int = 64) -> Image.Image:
    """A 64x64 RGBA render with each colour scaled by its own alpha.

    RGB underneath a fully transparent pixel is undefined and encoders rewrite
    it freely -- libwebp does exactly that without ``exact=True``, owner rule
    1's trap. Comparing raw channels therefore sees differences of the full
    0..255 range in pixels invisible in both images. Multiplying by alpha
    collapses every transparent pixel to the same value, so only what can
    actually be seen is compared.
    """
    img = Image.open(path).convert("RGBA").resize((n, n), Image.LANCZOS)
    r, g, b, a = img.split()
    return Image.merge("RGBA", (ImageChops.multiply(r, a),
                                ImageChops.multiply(g, a),
                                ImageChops.multiply(b, a), a))


def same_image(a: Path, b: Path, fmt: str) -> bool | None:
    """Do two files hold the same picture? None when it cannot be decided.

    ``content_key`` equality alone is the wrong question across a Telegram
    round trip: the key is a SHA of exact pixels, so a lossy re-encode changes
    it for a picture that is visually identical. Reading that as proof of a
    DIFFERENT image is what stopped a 449-emoji publish on a sticker that had
    landed perfectly well.

    Exact first, because when the re-encode happens to be pixel-exact that is
    the strongest answer available. Then structure AND colour, both of which
    must agree -- see the tolerances above for why either alone is unsafe.

    A false negative here costs a halted publish, which is recoverable; a false
    positive attributes a stranger's sticker to our item, which is not. The
    asymmetry is why this asks for two independent agreements rather than one.

    Animated is vector and has no raster hash, so a mismatch there is
    undecidable rather than negative -- unknown is not false. Callers read None
    as "reconcile", which is safe, and False as "a foreign sticker landed",
    which is not a claim this could honestly make from a comparison it could
    not perform.
    """
    try:
        if content_key(a, fmt) == content_key(b, fmt):
            return True
        ha, hb = perceptual_hash(a, fmt), perceptual_hash(b, fmt)
        if ha is None or hb is None:
            return None
        if hamming(ha, hb) > UPLOAD_PHASH_TOLERANCE:
            return False
        diff = ImageChops.difference(_premultiplied(a), _premultiplied(b))
        return sum(ImageStat.Stat(diff).mean) / 4.0 <= UPLOAD_MEAN_DELTA
    except Exception as exc:     # noqa: BLE001 - a failed probe is not a "no"
        log.debug("same_image(%s, %s) failed: %s", a.name, b.name, exc)
        return None


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
    """Hash normalized sampled frames so visually identical videos match.

    Resampling to a fixed 10 fps is what makes two encodes of the same clip
    agree. It also has one hole: a SINGLE-FRAME video is shorter than one
    sampling interval, so the filter emits NOTHING and the digest silently fell
    through to hashing the container bytes -- which is not a content key at all.
    Two such stickers differing only in container framing did not dedup, and
    re-encoding one (owner rule 1) moved its key, orphaning its catalog row.
    Real files: two single-frame .webm in the collector catalog.

    So an empty resample retries at the video's OWN frames before giving up.
    Raw bytes remain the last resort for a file ffmpeg cannot decode at all,
    where any key is better than none -- but that is now a decode failure, not
    an ordinary short clip.
    """
    frames = _video_frames_rgba(path)
    if frames:
        return hashlib.sha256(frames).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _video_frames_rgba(path: Path) -> bytes:
    """The normalized frame stream BOTH video identity keys are built from.

    One helper, not two copies of the ffmpeg call: fingerprint() used to carry
    its own, and when the single-frame retry was added to the digest alone the
    two silently disagreed -- the same file getting one key from content_key()
    and a different one from fingerprint(), which is the identity bug this
    project exists to avoid.

    Failure is NOT caught here. A hung or timed-out ffmpeg is "I could not
    look", and turning that into an empty sample would turn it into a byte
    hash -- a key that looks fine, never dedups, and hides the timeout.
    fingerprint() used to swallow it exactly that way. Let MediaError out;
    every ingest site already fails that one item and counts it.

    An EMPTY result from a SUCCESSFUL run is different: the file decoded, it
    just produced no frames at this sampling rate. That is the case the retry
    below is for, and the byte-hash fallback in the callers remains only for a
    file that genuinely renders nothing.
    """
    ff = media.ffmpeg_path()

    def sample(rate_filter: str) -> bytes:
        cmd = [ff, "-v", "error", "-t", str(media.WEBM_MAX_SECONDS), "-i", str(path),
               "-an", "-vf", f"{rate_filter}scale=64:64,format=rgba",
               "-f", "rawvideo", "-"]
        return media._run(cmd, capture=True).stdout or b""

    # fps=10 is what makes two encodes of the same clip agree. A single-frame
    # video is shorter than one sampling interval and yields NOTHING, so retry
    # at the file's own frames before giving up on a content key.
    return sample("fps=10,") or sample("")


def _animated_content_digest(path: Path) -> str:
    """Hash the canonicalized Lottie JSON so re-gzipped TGS files match."""
    try:
        lottie = media._load_lottie(path)
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
        raw = _video_frames_rgba(path)
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
    ff = media.ffmpeg_path()
    cmd = [ff, "-v", "error", "-i", str(path), "-frames:v", "1",
           "-vf", "scale=64:64,format=rgba", "-f", "rawvideo", "-"]
    res = media._run(cmd, capture=True)
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
