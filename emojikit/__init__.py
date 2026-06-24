"""Emoji Mapper core toolkit.

A small, layered, dependency-light core shared by the multi-format collector
workflow:

- ``logsetup``  -- mandatory UTC file logging.
- ``media``     -- format detection, content/perceptual hashing and conversion
                   of images/animations into Telegram custom-emoji media
                   (static PNG, animated TGS, video WEBM).
- ``catalog``   -- a persistent, content-addressed SQLite catalog that
                   deduplicates emoji at ingest time and tracks what has already
                   been uploaded (idempotent, resumable, duplicate-proof).

The toolkit is deliberately independent of any specific bot or workflow so the
same engine powers both "download from existing Telegram packs" and "build from
scratch" use cases.
"""

from __future__ import annotations

__all__ = ["logsetup", "media", "catalog"]
