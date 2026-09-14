"""Emoji Mapper core toolkit.

A shared library for the collector, publishing workflows and local curation:

- ``logsetup``  -- mandatory UTC file logging.
- ``media``     -- format detection, content/perceptual hashing and conversion
                   of images/animations into Telegram custom-emoji media
                   (static PNG, animated TGS, video WEBM).
- ``catalog``   -- a persistent, content-addressed SQLite catalog that
                   deduplicates emoji at ingest time and tracks what has already
                   been uploaded (idempotent, resumable, duplicate-proof).

Run the command-line modules from the project root with ``python -m emojikit.NAME``.
State, API, migration, gallery and panel implementations share this package.
"""

from __future__ import annotations

__all__ = ["logsetup", "media", "catalog"]
