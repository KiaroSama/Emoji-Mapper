"""Exception types shared across emojikit.

Its own module because `video_decode` has to RAISE one and `media` imports
`video_decode`: defining them in `media` and importing them back is the cycle
that made `video_decode` import `media` lazily in the first place. Nothing here
imports anything, so nothing can cycle through it.
"""

from __future__ import annotations


class MediaError(RuntimeError):
    """Raised when conversion or validation of a media file fails."""


class UndecodableVideo(MediaError):
    """A video's identity-grade decode could not be ESTABLISHED.

    Not the same claim as "this file is broken". It also covers "the toolchain
    could not prove it read the alpha" -- a probe that failed, a codec the
    container would not name, an ffmpeg with no decoder for a format that keeps
    transparency in a separate layer.

    It exists because the alternative is worse than an error. Degrading to a
    plain decode loses alpha silently, and two clips differing only in opacity
    then share one content key: `Catalog.add` merges on an equal key and
    `_drop_unreferenced` deletes the file it merged away. An unknown identity
    must never reach the catalog, so this is raised instead of guessed.

    A `MediaError` subclass on purpose: every ingest site already fails one item
    on `MediaError`/`RuntimeError` and counts it, so an undecodable video is
    skipped and reported rather than silently absorbed.
    """


class OperatorConfigMissing(RuntimeError):
    """A setting that names THIS operator (a bot, a pack base, a logo) is unset.

    The repository is public, so none of these has a default: a default would
    be somebody else's identity, published under yours. Unset is unknown, and
    the tool stops before changing anything instead of guessing.
    """
