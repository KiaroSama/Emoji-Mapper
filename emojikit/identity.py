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
from emojikit import media, video_decode

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


def _premultiplied(path: Path, n: int = 64, *, fmt: str = "static") -> Image.Image | None:
    """A 64x64 RGBA render with each colour scaled by its own alpha.

    RGB underneath a fully transparent pixel is undefined and encoders rewrite
    it freely -- libwebp does exactly that without ``exact=True``, owner rule
    1's trap. Comparing raw channels therefore sees differences of the full
    0..255 range in pixels invisible in both images. Multiplying by alpha
    collapses every transparent pixel to the same value, so only what can
    actually be seen is compared.
    """
    if fmt == "video":
        # Pillow cannot open a .webm at all. This used to be an unconditional
        # `Image.open`, so the colour half of same_image() raised for EVERY
        # video pair and the result was permanently "undecidable" -- a
        # re-encoded video could never be confirmed to be our own sticker.
        img = _first_video_frame(path)
        if img is None:
            return None
    else:
        img = Image.open(path).convert("RGBA")
    img = img.convert("RGBA").resize((n, n), Image.LANCZOS)
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
        pa, pb = _premultiplied(a, fmt=fmt), _premultiplied(b, fmt=fmt)
        if pa is None or pb is None:
            return None          # could not look; unknown is not false
        diff = ImageChops.difference(pa, pb)
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
    """The canonical sample stream. See `emojikit.video_decode` for the rules.

    Kept as a name here because content_key, fingerprint, perceptual_hash and
    same_image all reach for it; the decoding itself lives in one module so the
    alpha-capable decoder and the sampling rate cannot drift apart again.
    """
    return video_decode.frames_rgba(path)


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
            if img is None:
                return None
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
        phash = None
        try:
            img = _frame_image(raw)
            if img is not None:
                phash = _dhash(img)
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


def _first_video_frame(path: Path) -> Image.Image | None:
    """Frame zero of the canonical stream, decoded once.

    This used to run its OWN ffmpeg with `-frames:v 1` and no fps filter, which
    is a different frame from the one `fingerprint` hashes: a 30 fps clip with a
    distinct opening frame measured 6510615555426900570 here and
    5300177295401782857 there, for the same file through two public APIs (F13).
    Both now slice the same bytes, so they cannot disagree.
    """
    return _frame_image(video_decode.frames_rgba(path))


def _frame_image(raw: bytes) -> Image.Image | None:
    """Wrap frame zero of a canonical stream, or None if there is not one."""
    frame = video_decode.first_frame_bytes(raw)
    if frame is None:
        return None
    return Image.frombytes("RGBA", (video_decode.FRAME_SIDE, video_decode.FRAME_SIDE), frame)


def _dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Difference hash: compare adjacent pixels of a (hash_size+1) x hash_size gray image.

    The image is premultiplied by its own alpha first. ``convert("L")`` on an
    RGBA image DISCARDS alpha and reads the raw RGB, and RGB underneath a fully
    transparent pixel is undefined -- encoders rewrite it freely, which is the
    same trap ``_premultiplied`` and owner rule 1's ``exact=True`` exist for.
    Without this, one logo measured 10 bits away from Telegram's re-encode of
    ITSELF (tolerance 6) while its alpha channel was byte-identical and its
    premultiplied colour delta was 1.23; premultiplied, the distance is 0 and a
    genuinely different image still measures 23.

    A fully opaque image is unaffected: premultiplying by 255 is the identity.
    """
    if img.mode == "RGBA":
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                   ImageChops.multiply(g, a),
                                   ImageChops.multiply(b, a), a))
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
