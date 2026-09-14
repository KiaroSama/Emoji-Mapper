"""Emoji Mapper core toolkit.

A shared library for the collector, publishing workflows and local curation:

- ``logsetup``  -- mandatory UTC file logging.
- ``media``     -- format detection, content/perceptual hashing and conversion
                   of images/animations into Telegram custom-emoji media
                   (static PNG, animated TGS, video WEBM).
- ``catalog``   -- a persistent, content-addressed SQLite catalog that
                   deduplicates emoji at ingest time and tracks what has already
                   been uploaded (idempotent, resumable, duplicate-proof).

The command-line entry points stay at the project root. State, API, migration,
gallery and panel helpers live here, so their implementations have one home.
"""

from __future__ import annotations

__all__ = ["logsetup", "media", "catalog"]
