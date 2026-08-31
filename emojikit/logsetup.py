"""Advanced, secret-safe logging for Emoji Mapper executable scripts.

Every execution creates a fresh UTC log file under the project ``logs/``
directory, named ``<script>_YYYY-MM-DD_HH-mm-ss_UTC_<run_id>.log`` (resolved
relative to the project root, not the caller's CWD). Timestamps are UTC to the
second (no milliseconds).

Features
--------
* **Per-run id** — a short id stamped on every line, so interleaved/streamed
  logs are attributable to one execution.
* **Automatic secret redaction** — known secret *values* (from ``.env``-style
  env vars) and token-shaped strings are masked in *every* emitted record,
  including exception tracebacks. Nothing token-shaped reaches a file.
* **Rich file format** — UTC time, level, logger, ``module:line`` and the run id;
  a concise (optionally colored) console format.
* **Uncaught-exception capture** — ``sys.excepthook`` and the threading hook log
  full tracebacks as CRITICAL.
* **Run summary** — at interpreter exit, a summary line reports duration and the
  number of warnings/errors/criticals plus the log path.
* **Quiet third parties** — ``urllib3``/``requests``/``PIL`` are turned down so
  bot-token URLs and decoder chatter never flood (or leak into) the log.

Backwards compatible: ``setup_logging(name)`` and ``redact(text)`` keep working.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from build_pack import safe_int_env

# Project root = parent of this package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_RETENTION_DEFAULT_DAYS = 30


def log_retention_days() -> int:
    """Resolve the retention window per call, not at import.

    ``load_env()`` runs inside main(), which is *after* this module is imported,
    so a value set only in .env was read strictly too late and every run silently
    used the built-in default -- while .env.example advertised the setting as
    supported. Same hazard, and the same fix, as build_pack.api_base().

    Parsed defensively: a bare int() here turned one stray character in .env into
    an ImportError, killing logging (and the script) before startup could report
    anything. Negative values are clamped so "-1" cannot mean "prune everything".
    """
    return safe_int_env("EMOJI_LOG_RETENTION_DAYS",
                        LOG_RETENTION_DEFAULT_DAYS, minimum=0)

# Env vars whose *values* are secrets and must be masked wherever they appear.
SECRET_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "GENERAL_BOT_TOKEN", "CMC_API_KEY",
                   "BOT_TOKEN", "API_KEY", "TOKEN")

# Token-shaped patterns (catch secrets even if not registered as a value).
_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_BOT_URL_RE = re.compile(r"/bot\d{6,}:[A-Za-z0-9_-]{20,}/")

# Registered literal secret values (longest-first matching is applied).
_SECRETS: set[str] = set()

# Per-run state.
_RUN: dict = {"id": None, "start": None, "log_path": None,
              "counts": {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}}

_LEVEL_COLOR = {  # ANSI for TTY consoles
    "DEBUG": "\033[38;5;244m", "INFO": "\033[38;5;39m", "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;203m", "CRITICAL": "\033[1;37;41m",
}
_RESET = "\033[0m"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def register_secret(value: str | None) -> None:
    """Register a literal secret value to be masked in all future log output."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Mask registered secret values and token-shaped strings."""
    if not text:
        return text
    for sec in sorted(_SECRETS, key=len, reverse=True):
        if sec in text:
            text = text.replace(sec, "[REDACTED]")
    text = _BOT_URL_RE.sub("/bot[REDACTED]/", text)
    return _TOKEN_RE.sub("[REDACTED]", text)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "script"


# --------------------------------------------------------------------------- #
# Formatters / handlers
# --------------------------------------------------------------------------- #
class _HumanFormatter(logging.Formatter):
    """UTC, redacted, optionally colored human-readable formatter."""

    def __init__(self, fmt: str, *, color: bool = False):
        super().__init__(fmt)
        self.color = color

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    def format(self, record: logging.LogRecord) -> str:
        record.run_id = _RUN["id"] or "--------"
        s = redact(super().format(record))
        if self.color:
            c = _LEVEL_COLOR.get(record.levelname, "")
            if c:
                s = f"{c}{s}{_RESET}"
        return s


class _CounterHandler(logging.Handler):
    """Counts WARNING/ERROR/CRITICAL records for the end-of-run summary."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelname in _RUN["counts"]:
            _RUN["counts"][record.levelname] += 1


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def setup_logging(script_name: str, *, console_level: int = logging.INFO,
                  file_level: int = logging.DEBUG,
                  color: bool | None = None) -> logging.Logger:
    """Configure root logging (console + fresh UTC file). Idempotent per process.

    ``color`` forces ANSI colors on/off for the console (default: auto by TTY).
    """
    logger = logging.getLogger()
    if getattr(logger, "_emojikit_configured", False):
        return logger
    logger.setLevel(logging.DEBUG)

    _RUN["id"] = secrets.token_hex(4)
    _RUN["start"] = time.time()

    # Register secret values from the environment so they are always masked.
    for key in SECRET_ENV_KEYS:
        register_secret(os.environ.get(key))

    if color is None:
        color = bool(getattr(sys.stderr, "isatty", lambda: False)()) and os.name != "nt"

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(_HumanFormatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", color=color))
    logger.addHandler(console)
    logger.addHandler(_CounterHandler())

    file_fmt = _HumanFormatter(
        "[%(asctime)s] [%(levelname)s] [%(run_id)s] [%(name)s] "
        "%(module)s:%(lineno)d %(message)s")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
        base = _sanitize(script_name)
        path = LOG_DIR / f"{base}_{stamp}_{_RUN['id']}.log"
        n = 1
        while path.exists():
            path = LOG_DIR / f"{base}_{stamp}_{_RUN['id']}_{n}.log"
            n += 1
        fileh = logging.FileHandler(path, encoding="utf-8")
        fileh.setLevel(file_level)
        fileh.setFormatter(file_fmt)
        logger.addHandler(fileh)
        _RUN["log_path"] = path
    except OSError as exc:
        logger.warning("File logging unavailable (%s); console only.", exc)

    logger._emojikit_configured = True  # type: ignore[attr-defined]

    # Quiet noisy third parties (and keep bot-token URLs out of the log).
    for noisy in ("urllib3", "requests", "urllib3.connectionpool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.INFO)
    logging.captureWarnings(True)

    _install_excepthooks(logger)
    _install_summary(logger)

    # Startup context block.
    logger.info("=== %s started | run %s ===", base, _RUN["id"])
    logger.info("Python %s on %s (%s) | pid %d", sys.version.split()[0],
                sys.platform, os.name, os.getpid())
    logger.info("Project root: %s", PROJECT_ROOT)
    logger.info("Args: %s", redact(" ".join(sys.argv)))
    if _RUN["log_path"]:
        logger.info("Log file: %s", _RUN["log_path"])
    logger.debug("CWD: %s", os.getcwd())
    pruned = prune_old_logs()
    if pruned:
        logger.debug("pruned %d log file(s) older than %d days",
                     pruned, log_retention_days())
    return logger


def _install_excepthooks(logger: logging.Logger) -> None:
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            prev(exc_type, exc, tb)
            return
        logger.critical("UNCAUGHT %s", exc_type.__name__,
                        exc_info=(exc_type, exc, tb))
        # Render the traceback ourselves instead of delegating to ``prev``:
        # the default hook prints it raw, and a requests exception carries the
        # full bot-token URL. Redacting the log but not stderr leaks it anyway.
        sys.stderr.write(redact("".join(
            traceback.format_exception(exc_type, exc, tb))))

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        def thook(args):
            logger.critical("UNCAUGHT in thread %s: %s", args.thread.name if args.thread else "?",
                            args.exc_type.__name__,
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = thook


def _install_summary(logger: logging.Logger) -> None:
    def summary():
        dur = time.time() - (_RUN["start"] or time.time())
        c = _RUN["counts"]
        # The exit code is the one thing an operator reading a log after the
        # fact always wants and could not previously find in it.
        code = _RUN.get("exit_code")
        code_txt = "?" if code is None else str(code)
        outcome = ("OK" if code == 0 else
                   "UNKNOWN" if code is None else f"FAILED({code_txt})")
        logger.info("=== run %s finished in %.2fs | %s | exit=%s | "
                    "warnings=%d errors=%d critical=%d ===",
                    _RUN["id"], dur, outcome, code_txt,
                    c["WARNING"], c["ERROR"], c["CRITICAL"])
        logging.shutdown()
    atexit.register(summary)


def record_exit_code(code: int) -> int:
    """Remember a command's exit code so the run summary can report it."""
    _RUN["exit_code"] = int(code)
    return code


def prune_old_logs(keep_days: int | None = None) -> int:
    """Delete run logs older than ``keep_days``; returns how many were removed.

    Every execution writes a fresh file, so an unattended box accumulated them
    forever. Failures here are ignored: log housekeeping must never break a run.

    ``keep_days`` defaults to None rather than to the module constant it used to
    bind: a default argument is evaluated at import, which is before load_env()
    has read .env, so the configured window never reached this function.
    """
    if keep_days is None:
        keep_days = log_retention_days()
    if keep_days <= 0 or not LOG_DIR.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in LOG_DIR.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
