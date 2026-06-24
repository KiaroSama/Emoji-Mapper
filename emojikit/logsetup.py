"""Mandatory UTC file logging for Emoji Mapper executable scripts.

Every execution creates a new log file named
``<script>_YYYY-MM-DD_HH-mm-ss_UTC.log`` under the project ``logs/`` directory
(resolved relative to the project root, not the caller's CWD). Timestamps are
UTC to the second (no milliseconds). Bot tokens and other secrets must be
redacted with :func:`redact` before logging.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Project root = parent of this package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

# Telegram bot tokens look like 1234567890:AA... ; CMC keys are long hex. Redact
# anything token-shaped so it can never leak into a log file.
_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_BOT_URL_RE = re.compile(r"/bot\d{6,}:[A-Za-z0-9_-]{20,}/")


class _UtcFormatter(logging.Formatter):
    """Formatter that emits UTC timestamps to the second (no milliseconds)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def redact(text: str) -> str:
    """Mask token-shaped secrets in a string before it is logged or printed."""
    text = _BOT_URL_RE.sub("/bot[REDACTED]/", text)
    return _TOKEN_RE.sub("[REDACTED]", text)


def _sanitize(name: str) -> str:
    """Make a script name safe for use inside a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "script"


def setup_logging(script_name: str, *, console_level: int = logging.INFO,
                  file_level: int = logging.DEBUG) -> logging.Logger:
    """Configure root logging with a console handler and a fresh UTC file handler.

    Returns the configured root logger. Safe to call once per process; repeated
    calls reuse the existing handlers to avoid duplicate log lines.
    """
    logger = logging.getLogger()
    if getattr(logger, "_emojikit_configured", False):
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = _UtcFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File logging must not crash the program if the directory is unwritable;
    # fall back to console-only and report the failure clearly.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
        base = _sanitize(script_name)
        path = LOG_DIR / f"{base}_{stamp}.log"
        n = 1
        while path.exists():  # never overwrite a previous execution's log
            path = LOG_DIR / f"{base}_{stamp}_{n}.log"
            n += 1
        fileh = logging.FileHandler(path, encoding="utf-8")
        fileh.setLevel(file_level)
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
        logger.info("Logging to %s", path)
    except OSError as exc:
        logger.warning("File logging unavailable (%s); console only.", exc)

    logger._emojikit_configured = True  # type: ignore[attr-defined]
    # Record a little environment context up front for diagnostics.
    logger.info("Python %s on %s", sys.version.split()[0], sys.platform)
    logger.info("Project root: %s", PROJECT_ROOT)
    logger.debug("UTC start: %s", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    return logger
