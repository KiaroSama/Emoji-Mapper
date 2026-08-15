"""Create Telegram premium custom-emoji pack(s) from a folder of PNG images.

This is the generic Emoji Mapper engine: it uploads every 100x100 PNG found in a
source directory into one or more Telegram custom-emoji sets. It is NOT tied to
cryptocurrency coins -- point ``--source-dir`` at any folder of prepared PNGs
(see make_emoji_pngs.py) to build a pack of arbitrary emojis.

A bot can create a custom-emoji set OWNED by a user, so you need:
  - a bot token (env or .env)                -> selected with --token-env
  - PACK_OWNER_USER_ID  (env, or --user-id)  -> your numeric Telegram user id
  - You must have pressed Start on that bot at least once.

Two bots are configured by default:
  - TELEGRAM_BOT_TOKEN  -> the original crypto-coin bot (default)
  - GENERAL_BOT_TOKEN   -> @YourEmojiBot, for general (non-coin) packs

Telegram limits each custom-emoji set to 200 emojis, so the images are split
into multiple sets named ``<base><n>_by_<botusername>``. Each emoji is given an
associated standard emoji (--emoji) and optional searchable keywords (from a
keywords.csv mapping ``ticker -> keywords``; falls back to the file name).

Usage (crypto coins, original bot):
  python build_pack.py --base cryptoemoji --title "@YourBrand Crypto Emoji" \
      [--user-id 123] [--emoji ߞ] [--limit N] [--start N] [--dry-run]

Usage (general pack, new bot):
  python build_pack.py --base mystickers --title "My Emojis" \
      --source-dir build/myset --token-env GENERAL_BOT_TOKEN --emoji ߘ

Run with --dry-run first to validate inputs without calling Telegram.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


# Exit codes shared by every CLI entry point, so a launcher or CI job can tell
# "done", "bad input", "retry me" and "stop" apart. Returning 0 after total
# failure made retry logic and menu actions treat a dead run as a success.
EXIT_OK = 0
EXIT_USAGE = 2       # invalid arguments or configuration
EXIT_PARTIAL = 3     # some items succeeded, some failed -- retryable
EXIT_FAILED = 4      # nothing succeeded, or an integrity stop


def ingest_exit_code(succeeded: int, failed: int) -> int:
    """Exit code for a batch that processed ``succeeded`` and ``failed`` items."""
    if not failed:
        return EXIT_OK
    return EXIT_PARTIAL if succeeded else EXIT_FAILED


def _intent_key(intent) -> str | None:
    """The item key of an in-flight intent, accepting the legacy bare string."""
    if isinstance(intent, dict):
        return intent.get("key")
    return intent or None


def make_intent(*, key: str, operation: str, set_name: str, set_index: int,
                expected_before: int | None, title: str = "",
                fmt: str = "static") -> dict:
    """Structured record of a mutation that is about to be attempted.

    A bare item key is not enough to reconcile an ambiguous CREATE: the set it
    would have created is not yet in the state's set list, so a restart has no
    name to probe. Recording the operation and its target makes every
    unresolved mutation reconcilable.
    """
    return {"key": key, "operation": operation, "set_name": set_name,
            "set_index": set_index, "expected_before": expected_before,
            "title": title, "format": fmt, "started_utc": _utc_now()}


def safe_int_env(name: str, default: int = 0, *, minimum: int | None = None,
                 maximum: int | None = None) -> int:
    """Parse a numeric env var without letting a typo kill the process.

    ``int(os.environ.get(...))`` at import time turns one bad character in .env
    into an unexplained crash before argparse can print anything useful.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"WARNING: {name} is not a whole number; using {default}.",
              file=sys.stderr)
        return default
    if minimum is not None and value < minimum:
        print(f"WARNING: {name}={value} below {minimum}; using {minimum}.",
              file=sys.stderr)
        return minimum
    if maximum is not None and value > maximum:
        print(f"WARNING: {name}={value} above {maximum}; using {maximum}.",
              file=sys.stderr)
        return maximum
    return value


class LockBusy(RuntimeError):
    """Another process already holds this publisher's lock."""


@contextlib.contextmanager
def exclusive_lock(path: Path, *, stale_after: float = 6 * 3600):
    """Exclusive per-state lock so two publishers cannot mutate one pack family.

    Without it, two runs read the same state, both see the same item pending,
    and both upload it. Uses O_EXCL creation, which is atomic on NTFS and
    POSIX alike; a lock left by a crashed run is reclaimed after ``stale_after``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime if path.exists() else 0.0
            if age < stale_after:
                holder = ""
                try:
                    holder = path.read_text(encoding="utf-8").strip()[:120]
                except OSError:
                    pass
                raise LockBusy(
                    f"{path.name} is held by another run ({holder or 'unknown'}; "
                    f"{age:.0f}s old). Refusing to publish concurrently.")
            print(f"  reclaiming stale lock {path.name} ({age:.0f}s old)",
                  flush=True)
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid={os.getpid()} started={_utc_now()}\n".encode())
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        path.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def write_json_atomic(path: Path, data) -> None:
    """Write JSON so an interrupted run can never leave a truncated file.

    Upload progress lives in these files: a half-written state file is read back
    as corrupt (or, worse, silently replaced by an empty default) and the whole
    source set gets uploaded again. Write to a sibling temp file, flush it to
    disk, then rename -- rename is atomic on both NTFS and POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
# Default source/keyword locations (crypto-coin workflow). Override per run with
# --source-dir / --keywords so the same engine builds any kind of emoji pack.
EMOJI_DIR = ROOT / "logos" / "emoji"
KEYWORDS_CSV = ROOT / "keywords.csv"
# Legacy default state file (kept for backwards compatibility); the actual state
# file used by a run defaults to state_<base>.json so different packs never
# clobber each other.
STATE_FILE = ROOT / "pack_state.json"

# Custom emoji must be 100x100 PNG; build_pack uploads the prepared PNGs.
_MIME = {".png": "image/png", ".webp": "image/webp"}
DEFAULT_EMOJI = "\U0001FA99"  # ߞ coin
# Telegram's hard cap for a custom-emoji set. See
# https://core.telegram.org/bots/api#addstickertoset -- "Emoji sticker sets can
# have up to 200 stickers." Exceeding it only produces STICKERS_TOO_MUCH at
# upload time, after the wrong set count has already been planned.
MAX_PER_SET = 200
PER_SET = MAX_PER_SET
# Upper bound on how long a create may wait for a deleted set name to be
# released, across all of its retries.
NAME_LOCK_TIMEOUT = 300.0


def api_base() -> str:
    """Resolve the API base per call, not at import.

    ``load_env()`` runs inside main(), which is *after* this module is imported,
    so reading the env var at import time silently ignores a TELEGRAM_API_BASE
    that is configured only in .env.
    """
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def links_chat_id(owner_id: int) -> str | int:
    """Where finished-pack links are announced.

    ``PACK_LINKS_CHAT_ID`` may be a channel id (``-100...``) or an ``@name``.
    Falls back to the owner's private chat so existing setups keep working.
    The bot must be an administrator of that channel to post in it.
    """
    raw = os.environ.get("PACK_LINKS_CHAT_ID", "").strip()
    if not raw:
        return owner_id
    return raw if raw.startswith("@") else int(raw)


def load_env() -> None:
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_keywords(path: Path = KEYWORDS_CSV) -> dict[str, str]:
    """ticker -> 'ticker, name' keyword string (optional; missing file -> {})."""
    out: dict[str, str] = {}
    if path and Path(path).is_file():
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out[row["ticker"].lower()] = row.get("keywords") or row["ticker"]
    return out


class SetState(Enum):
    """Three distinct answers to "does this sticker set exist?".

    Collapsing UNKNOWN into MISSING is how a network blip becomes "the set is
    empty", which then justifies re-uploading or re-creating it.
    """

    EXISTS = "exists"
    MISSING = "missing"
    UNKNOWN = "unknown"


class LiveStateUnknown(RuntimeError):
    """Live Telegram state could not be determined; callers must not guess."""


class AmbiguousUploadError(RuntimeError):
    """A state-changing call failed at the network level and the live state
    could not be verified: the change may or may not have been applied.
    Callers must reconcile against live Telegram state instead of re-sending
    (re-sending a non-idempotent call such as addStickerToSet would DUPLICATE
    its effect)."""


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.s = requests.Session()

    def _safe(self, exc: BaseException) -> str:
        """Exception text with the bot token stripped.

        Every request URL embeds the token, and requests puts the URL in its
        exception message -- printing ``str(exc)`` raw publishes the token to
        the console and, via the coin runner's output redirect, to a log file.
        """
        return str(exc).replace(self.token, "[REDACTED]")

    def _call(self, method: str, *, data=None, files=None, retries: int = 5,
              applied_check=None):
        """POST a Bot API method with retries.

        ``applied_check`` makes retries safe for NON-idempotent methods
        (addStickerToSet / createNewStickerSet). After a network-level failure
        (the request may have been processed even though the response never
        arrived) it probes the live state and returns:
          True  -> the change IS live: report success, never re-send;
          False -> definitely not applied: safe to re-send;
          None  -> live state unknown: raise AmbiguousUploadError so the
                   caller reconciles instead of guessing.
        Without a check, such methods keep the historical blind-retry behavior.
        """
        url = f"{api_base()}/bot{self.token}/{method}"
        # A just-deleted set name stays locked for ~2 min, so a CREATE may
        # legitimately need to wait it out. For every other method
        # STICKERSET_INVALID means "no such set" -- a permanent answer that must
        # be returned at once, not slept on for six minutes.
        name_lock_retry = method == "createNewStickerSet"
        deadline = time.monotonic() + NAME_LOCK_TIMEOUT
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(url, data=data, files=files, timeout=60)
                try:
                    payload = r.json()
                except ValueError as exc:
                    # A proxy or gateway can answer with an HTML error page.
                    # That is a transport failure, not a Bot API reply, so it
                    # must go through the same retry/applied_check path as any
                    # other network error instead of escaping raw.
                    raise requests.exceptions.InvalidJSONError(
                        f"non-JSON response (HTTP {r.status_code})") from exc
                if payload.get("ok"):
                    return payload["result"]
                desc = str(payload.get("description", ""))
                # Honor flood waits.
                if "retry after" in desc.lower():
                    wait = int(payload.get("parameters", {}).get("retry_after", 5))
                    print(f"  flood wait {wait}s ({method})", flush=True)
                    time.sleep(wait + 1)
                    continue
                if "stickerset_invalid" in desc.lower():
                    remaining = deadline - time.monotonic()
                    if not name_lock_retry or attempt >= retries or remaining <= 0:
                        raise RuntimeError(f"{method} failed: {desc}")
                    wait = min(30 * attempt, 90, remaining)
                    print(f"  stickerset_invalid; name not released yet, "
                          f"wait {wait:.0f}s ({method})", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} failed: {desc}")
            except requests.RequestException as exc:
                if applied_check is not None:
                    time.sleep(2)  # let Telegram settle before probing
                    applied = applied_check()
                    if applied is True:
                        print(f"  {method}: network error but the change is "
                              f"verified live; not re-sending", flush=True)
                        return {"verified_applied": True}
                    if applied is None:
                        raise AmbiguousUploadError(
                            f"{method}: network failure and live state "
                            f"unknown ({self._safe(exc)})") from exc
                    # applied is False: definitely not applied, safe to re-send.
                if attempt >= retries:
                    break          # never sleep after the final attempt
                wait = min(3 * attempt, 20)
                print(f"  net retry {attempt}/{retries} ({method}): "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"{method} failed after {retries} attempts")

    def probe_sticker_set(self, name: str) -> tuple[bool, dict | None]:
        """Single, non-retrying live probe of a sticker set.

        Returns ``(known, set)``: ``(True, dict)`` it exists, ``(True, None)``
        it definitely does not exist, ``(False, None)`` live state unknown
        (network/API failure). Unlike getStickerSet via ``_call`` this never
        raises and never sleeps (``_call`` treats STICKERSET_INVALID as a
        name-release lock and waits minutes, which a probe must not do).
        """
        try:
            r = self.s.post(f"{api_base()}/bot{self.token}/getStickerSet",
                            data={"name": name}, timeout=30)
            payload = r.json()
        except (requests.RequestException, ValueError):
            return False, None
        if payload.get("ok"):
            return True, payload["result"]
        if "stickerset_invalid" in str(payload.get("description", "")).lower():
            return True, None
        return False, None

    def probe_set_state(self, name: str) -> tuple[SetState, dict | None]:
        """Tri-state probe: EXISTS / MISSING / UNKNOWN plus the set when known."""
        known, sset = self.probe_sticker_set(name)
        if not known:
            return SetState.UNKNOWN, None
        return (SetState.EXISTS, sset) if sset is not None else (SetState.MISSING, None)

    def live_count_strict(self, name: str) -> int:
        """Live sticker count, or raise LiveStateUnknown.

        Never returns 0 for "could not tell": a caller that rolls back or
        retries a mutation on that 0 will re-send an upload that already
        landed.
        """
        state, sset = self.probe_set_state(name)
        if state is SetState.EXISTS:
            return len(sset.get("stickers", []))
        if state is SetState.MISSING:
            return 0
        raise LiveStateUnknown(f"live state of {name} is unknown")

    def _added_check(self, name: str, expected_before: int | None):
        """applied_check for addStickerToSet: did the set grow by exactly one?"""
        if expected_before is None:
            return None

        def check():
            known, sset = self.probe_sticker_set(name)
            if not known or sset is None:
                return None  # unknown / set vanished: reconcile, don't guess
            n = len(sset.get("stickers", []))
            if n == expected_before + 1:
                return True
            if n == expected_before:
                return False
            return None  # count drifted: reconcile, don't guess

        return check

    def _created_check(self, name: str):
        """applied_check for createNewStickerSet: does the set now exist?"""
        def check():
            known, sset = self.probe_sticker_set(name)
            if not known:
                return None
            return sset is not None

        return check

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: int | str, text: str) -> None:
        self._call("sendMessage", data={
            "chat_id": chat_id, "text": text, "disable_web_page_preview": False,
        })

    # NOTE: all upload methods pass the file CONTENT (bytes), not an open
    # handle: a retried request must re-send the full body, and a file object
    # is already exhausted after the first attempt (a flood-wait or network
    # retry would silently send an empty file and fail the sticker).

    def upload_sticker(self, user_id: int, path: Path) -> str:
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        res = self._call(
            "uploadStickerFile",
            data={"user_id": user_id, "sticker_format": "static"},
            files={"sticker": (path.name, path.read_bytes(), mime)},
        )
        return res["file_id"]

    def create_set(self, user_id: int, name: str, title: str, png: Path,
                   emoji: str, keywords: str) -> None:
        # Upload the image inline via attach:// (1 request instead of 2).
        self._call("createNewStickerSet", data={
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_sticker_json(emoji, keywords)]),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=self._created_check(name))

    def add_sticker(self, user_id: int, name: str, png: Path,
                    emoji: str, keywords: str, *,
                    expected_before: int | None = None) -> None:
        """Add a static sticker. Pass ``expected_before`` (the live sticker
        count the caller expects BEFORE this add) to make network retries
        duplicate-proof; without it the historical blind retry is kept."""
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_sticker_json(emoji, keywords)),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=self._added_check(name, expected_before))

    # ----- multi-format helpers (static / animated / video) -------------- #
    def get_sticker_set(self, name: str) -> dict:
        """Return the full Bot API StickerSet object for a set short name."""
        return self._call("getStickerSet", data={"name": name})

    def get_custom_emoji_stickers(self, custom_emoji_ids: list[str]) -> list[dict]:
        """Resolve custom-emoji IDs to their Sticker objects (max 200 per call).

        Any bot can resolve arbitrary ``custom_emoji_id`` values; the Bot API
        silently drops IDs it cannot find, so the returned list may be shorter
        than the input. Each returned Sticker carries ``custom_emoji_id``,
        ``file_id`` and ``file_unique_id`` plus the animated/video flags.
        """
        if not custom_emoji_ids:
            return []
        if len(custom_emoji_ids) > 200:
            raise ValueError("getCustomEmojiStickers accepts at most 200 IDs per call")
        return self._call("getCustomEmojiStickers",
                          data={"custom_emoji_ids": json.dumps(custom_emoji_ids)})

    def download_file(self, file_id: str, dest: Path, retries: int = 5) -> Path:
        """Download a Telegram file (by file_id) to ``dest`` (with retries)."""
        info = self._call("getFile", data={"file_id": file_id})
        url = f"{api_base()}/file/bot{self.token}/{info['file_path']}"
        for attempt in range(1, retries + 1):
            try:
                r = self.s.get(url, timeout=60)
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                return dest
            except requests.RequestException as exc:
                if attempt >= retries:
                    break
                wait = min(3 * attempt, 15)
                print(f"  download retry {attempt}/{retries}: "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"download failed for file_id {file_id}")

    def create_emoji_set(self, user_id: int, name: str, title: str, path: Path,
                         fmt: str, emoji_list: list[str], keywords: list[str]) -> None:
        """Create a custom-emoji set whose first emoji is ``path`` (any format)."""
        self._call("createNewStickerSet", data={
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_input_sticker(fmt, emoji_list, keywords)]),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=self._created_check(name))

    def add_emoji(self, user_id: int, name: str, path: Path, fmt: str,
                  emoji_list: list[str], keywords: list[str], *,
                  expected_before: int | None = None) -> None:
        """Add one emoji (any format) to an existing custom-emoji set.

        ``expected_before`` (the live sticker count expected BEFORE this add)
        makes network retries duplicate-proof; see ``add_sticker``."""
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_input_sticker(fmt, emoji_list, keywords)),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=self._added_check(name, expected_before))


_MIME_BY_FORMAT = {
    "static": "image/png",
    "animated": "application/gzip",
    "video": "video/webm",
}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tgs": "application/gzip",
    ".webm": "video/webm",
}


def _mime_for(fmt: str) -> str:
    return _MIME_BY_FORMAT.get(fmt, "application/octet-stream")


def _mime_for_path(path: Path) -> str:
    """MIME type derived from the file's real extension (preferred for uploads)."""
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _trim_keywords(keywords: list[str]) -> list[str]:
    """Clamp a keyword list to Telegram's per-sticker budget (<=20, ~64 chars)."""
    kw: list[str] = []
    total = 0
    for raw in keywords:
        k = (raw or "").strip()[:48]
        if not k:
            continue
        if kw and total + len(k) + 1 > 60:
            break
        kw.append(k)
        total += len(k) + 1
        if len(kw) >= 20:
            break
    return kw


def _input_sticker(fmt: str, emoji_list: list[str], keywords: list[str]) -> dict:
    """Build a Bot API InputSticker for any custom-emoji format (uploaded as file0)."""
    emojis = [e for e in (emoji_list or []) if e][:20] or [DEFAULT_EMOJI]
    return {"sticker": "attach://file0", "format": fmt,
            "emoji_list": emojis, "keywords": _trim_keywords(keywords)}


def _sticker_json(emoji: str, keywords: str) -> dict:
    """Static InputSticker from a comma-separated keyword string."""
    return {"sticker": "attach://file0", "format": "static",
            "emoji_list": [emoji],
            "keywords": _trim_keywords(keywords.split(","))}


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="Set name base (letters/digits/_).")
    ap.add_argument("--title", required=True, help="Human-readable set title.")
    ap.add_argument("--user-id", type=int,
                    default=safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0))
    ap.add_argument("--emoji", default=DEFAULT_EMOJI, help="Associated standard emoji.")
    ap.add_argument("--per-set", type=int, default=PER_SET)
    ap.add_argument("--limit", type=int, default=0, help="Max images to add (0=all).")
    ap.add_argument("--start", type=int, default=0, help="Skip this many images first.")
    ap.add_argument("--source-dir", default=str(EMOJI_DIR),
                    help="Folder of 100x100 PNGs to upload (default: logos/emoji).")
    ap.add_argument("--keywords", default="auto",
                    help="keywords.csv (ticker->keywords). 'auto' uses keywords.csv "
                         "only for the default coin source; missing file is OK.")
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN",
                    help="Env var holding the bot token (e.g. GENERAL_BOT_TOKEN).")
    ap.add_argument("--state", default="",
                    help="Resume state file (default: state_<base>.json).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"ERROR: {args.token_env} not set (env or .env).", file=sys.stderr)
        return 2
    if not args.user_id:
        print("ERROR: provide --user-id or PACK_OWNER_USER_ID (your numeric Telegram id).",
              file=sys.stderr)
        return 2
    if not 1 <= args.per_set <= MAX_PER_SET:
        print(f"ERROR: --per-set must be between 1 and {MAX_PER_SET} "
              f"(Telegram's cap for a custom-emoji set); got {args.per_set}.",
              file=sys.stderr)
        return 2
    if args.limit < 0 or args.start < 0:
        print("ERROR: --limit and --start must not be negative.", file=sys.stderr)
        return 2

    # Per-base state file so coin and general packs never clobber each other.
    state_file = Path(args.state) if args.state else ROOT / f"state_{args.base}.json"

    # Source: prepared 100x100 emoji PNGs (run make_emoji_pngs.py first).
    source_dir = Path(args.source_dir)
    sources = sorted(source_dir.glob("*.png"))
    if args.start:
        sources = sources[args.start:]
    if args.limit:
        sources = sources[:args.limit]
    if not sources:
        print(f"ERROR: no PNGs in {source_dir}. Run make_emoji_pngs.py first.",
              file=sys.stderr)
        return 2

    # 'auto' loads the coin keywords.csv only for the default coin source dir; a
    # general pack uses no keywords unless --keywords points at a file.
    if args.keywords == "auto":
        kw_path = KEYWORDS_CSV if source_dir.resolve() == EMOJI_DIR.resolve() else None
    else:
        kw_path = Path(args.keywords)
    keywords = load_keywords(kw_path) if kw_path else {}

    # Dry run validates inputs WITHOUT calling Telegram (no network required).
    if args.dry_run:
        total = len(sources)
        n_sets = (total + args.per_set - 1) // args.per_set
        print(f"DRY RUN: token-env={args.token_env}  owner_user_id={args.user_id}  "
              f"source={source_dir}  images={total}  keywords={len(keywords)}", flush=True)
        print(f"DRY RUN: {total} images -> {n_sets} set(s) of up to {args.per_set}, "
              f"named {args.base}1_by_<bot> ...  state={state_file.name}", flush=True)
        return 0

    # Two publishers on one state file both see the same item pending and both
    # upload it. Hold the lock for the whole mutation phase.
    lock_path = state_file.with_name(state_file.name + ".lock")
    try:
        lock = exclusive_lock(lock_path)
        lock.__enter__()
    except LockBusy as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        return _run_build(args, token, state_file, sources, keywords)
    finally:
        lock.__exit__(None, None, None)


def _run_build(args, token, state_file, sources, keywords) -> int:
    tg = Telegram(token)
    me = tg.get_me()
    bot_username = me["username"]

    # Resume support: load progress so an interrupted/flood-limited run can
    # continue without recreating existing sets or re-adding emojis.
    state = {"base": args.base, "per_set": args.per_set, "done": [], "sets": []}
    if state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Continuing from the empty default would treat every already
            # uploaded image as pending and upload the whole set a second time.
            print(f"ERROR: cannot read resume state {state_file}: {exc}\n"
                  f"       Refusing to start from scratch -- that would re-upload "
                  f"everything already published.\n"
                  f"       Inspect or delete the file deliberately, then re-run.",
                  file=sys.stderr)
            return 4
        # An existing state file that belongs to a DIFFERENT pack, or was
        # written with different settings, must not be silently ignored: doing
        # so restarts a published pack from zero and re-uploads everything.
        mismatches = []
        if loaded.get("base") != args.base:
            mismatches.append(f"base {loaded.get('base')!r} != {args.base!r}")
        if loaded.get("per_set") not in (None, args.per_set):
            mismatches.append(f"per_set {loaded.get('per_set')} != {args.per_set}")
        if mismatches:
            print(f"ERROR: resume state {state_file} does not match this run "
                  f"({'; '.join(mismatches)}).\n"
                  f"       Refusing to continue: starting from an empty state "
                  f"would re-upload an already published pack.\n"
                  f"       Use --state for a different file, or delete that one "
                  f"deliberately.", file=sys.stderr)
            return EXIT_FAILED
        state = loaded
    done = set(state["done"])
    sets = state["sets"]

    pending = [p for p in sources if p.stem.lower() not in done]

    # Resume safety. A previous run can have applied an upload without recording
    # it (crash, or an ambiguous network failure). Which image that was is known
    # exactly, because the intent is written BEFORE the request: state["in_flight"]
    # names it. Positional attribution -- "the first (live - counted) pending
    # images must be the unrecorded ones" -- is wrong as soon as any earlier
    # image was skipped for being missing/unusable, because a skip consumes a
    # position in `pending` without producing a sticker; that mis-attribution
    # both re-uploads a duplicate and marks the wrong image as done.
    in_flight = _intent_key(state.get("in_flight"))
    if sets:
        set_state, sset = tg.probe_set_state(sets[-1]["name"])
        if set_state is SetState.UNKNOWN:
            # The one thing we must not do is clear an unresolved intent because
            # the probe failed: that is how a mutation that DID land gets sent a
            # second time.
            print(f"ERROR: cannot determine the live state of "
                  f"{sets[-1]['name']}.\n"
                  f"       Refusing to continue while an upload may be "
                  f"unresolved. Retry when Telegram is reachable.",
                  file=sys.stderr)
            return EXIT_PARTIAL
        if set_state is SetState.EXISTS:
            live_n = len(sset.get("stickers", []))
            drift = live_n - sets[-1]["count"]
            if drift == 1 and in_flight:
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                sets[-1]["count"] = live_n
                print(f"  reconciled from live: {in_flight} was applied before "
                      f"the crash", flush=True)
                state["in_flight"] = None       # verified postcondition
            elif drift == 0 and in_flight:
                print(f"  {in_flight} did not land; it stays pending", flush=True)
                state["in_flight"] = None       # verified postcondition
            elif drift > 0:
                # More live stickers than we can account for: another run, a
                # manual edit, or a lost in-flight record. Guessing here is what
                # writes the wrong emoji id onto the wrong item.
                print(f"ERROR: {sets[-1]['name']} has {live_n} stickers but state "
                      f"records {sets[-1]['count']} and no single in-flight upload "
                      f"explains the difference.\n"
                      f"       Refusing to guess which images are already live. "
                      f"Reconcile the set manually, or delete it and re-run.",
                      file=sys.stderr)
                return EXIT_FAILED
    elif in_flight:
        # An ambiguous CREATE: the set was never recorded in state["sets"], so
        # only the intent knows which set name to look for.
        intent = state.get("in_flight") or {}
        pending_set = intent.get("set_name") if isinstance(intent, dict) else None
        if not pending_set:
            print(f"ERROR: an upload of {in_flight} is unresolved but the intent "
                  f"does not name a target set, so it cannot be reconciled.\n"
                  f"       Delete {state_file.name} deliberately after checking "
                  f"Telegram.", file=sys.stderr)
            return EXIT_FAILED
        set_state, sset = tg.probe_set_state(pending_set)
        if set_state is SetState.UNKNOWN:
            print(f"ERROR: cannot determine whether {pending_set} was created.\n"
                  f"       Refusing to continue while the create is unresolved.",
                  file=sys.stderr)
            return EXIT_PARTIAL
        if set_state is SetState.EXISTS:
            count = len(sset.get("stickers", []))
            sets.append({"name": pending_set, "title": intent.get("title", ""),
                         "count": count, "index": intent.get("set_index", 1)})
            done.add(in_flight)
            pending = [p for p in pending if p.stem.lower() != in_flight]
            print(f"  adopted {pending_set} created before the interruption "
                  f"({count} sticker(s))", flush=True)
        else:
            print(f"  create of {pending_set} did not land; {in_flight} stays "
                  f"pending", flush=True)
        state["in_flight"] = None               # verified postcondition

    print(f"Bot: @{bot_username}  owner_user_id={args.user_id}  "
          f"images={len(sources)}  already_done={len(done)}  pending={len(pending)}", flush=True)

    def save_state() -> None:
        state["done"] = sorted(done)
        state["sets"] = sets
        state["sent"] = sorted(sent)
        write_json_atomic(state_file, state)

    sent = set(state.get("sent", []))

    def notify(name: str, title: str) -> None:
        """Send the share/add link of a finished pack to the owner, once."""
        if name in sent:
            return
        try:
            tg.send_message(
                links_chat_id(args.user_id),
                f"\u2705 {title}\nhttps://t.me/addemoji/{name}",
            )
            sent.add(name)
            print(f"  sent link for {name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - never let notify break the build
            print(f"  notify failed for {name}: {exc}", flush=True)

    def notify_full_sets(final: bool = False) -> None:
        """Send links for finished packs, ALWAYS in ascending pack-number order.
        Any non-last set is complete; the last set counts as finished when full
        or when the whole run is done."""
        ordered = sorted(sets, key=lambda s: s["index"])
        last_index = max((s["index"] for s in sets), default=0)
        for s in ordered:
            if final or s["index"] != last_index or s["count"] >= args.per_set:
                notify(s["name"], s["title"])

    # Catch up: send links for any already-finished packs not yet sent.
    notify_full_sets()
    save_state()

    # Reconstruct the active set (last one that is not yet full).
    if sets and sets[-1]["count"] < args.per_set:
        set_index = sets[-1]["index"]
        set_name = sets[-1]["name"]
        in_set = sets[-1]["count"]
    else:
        set_index = len(sets)
        set_name = ""
        in_set = 0

    created = []
    failed: list[str] = []      # items this run could not upload
    uploaded = 0
    try:
        for i, path in enumerate(pending):
            ticker = path.stem.lower()
            kw = keywords.get(ticker, ticker)

            # Skip unusable files so one bad logo never stops the whole run.
            if not path.is_file() or path.stat().st_size == 0:
                print(f"  skip {ticker}: missing/empty file", flush=True)
                failed.append(ticker)
                continue

            # Write the intent BEFORE the request. If the process dies between
            # Telegram applying the upload and us recording it, the next run
            # knows exactly which image that was instead of inferring it from
            # positions (see the resume block above).
            if in_set != 0:
                state["in_flight"] = make_intent(
                    key=ticker, operation="add", set_name=set_name,
                    set_index=set_index, expected_before=in_set)
            else:
                next_index = set_index + 1
                state["in_flight"] = make_intent(
                    key=ticker, operation="create",
                    set_name=f"{args.base}{next_index}_by_{bot_username}",
                    set_index=next_index, expected_before=0,
                    title=f"{args.title} {next_index}")
            save_state()

            try:
                placed = False
                if in_set != 0:
                    try:
                        tg.add_sticker(args.user_id, set_name, path, args.emoji, kw,
                                       expected_before=in_set)
                        sets[-1]["count"] += 1
                        placed = True
                    except RuntimeError as exc:
                        # Set is full (count drift or 200-limit): roll to a new set.
                        if "STICKERS_TOO_MUCH" not in str(exc):
                            raise
                        in_set = 0
                if not placed:
                    set_index += 1
                    set_name = f"{args.base}{set_index}_by_{bot_username}"
                    title = f"{args.title} {set_index}"  # every pack is numbered
                    tg.create_set(args.user_id, set_name, title, path, args.emoji, kw)
                    sets.append({"name": set_name, "title": title, "count": 1,
                                 "index": set_index})
                    created.append(set_name)
                    print(f"[set {set_index}] created {set_name}", flush=True)
            except AmbiguousUploadError as exc:
                # The call may or may not be live. Try to settle it right here;
                # if it cannot be settled, STOP. Continuing to the next item
                # would overwrite this intent with the next one and destroy the
                # only record of which mutation is unresolved -- after which a
                # later run can re-send an upload that already landed.
                if not placed and in_set == 0:
                    set_state, sset = tg.probe_set_state(set_name)
                    if set_state is SetState.EXISTS and \
                            len(sset.get("stickers", [])) == 1:
                        sets.append({"name": set_name, "title": title, "count": 1,
                                     "index": set_index})
                        created.append(set_name)
                        state["in_flight"] = None   # verified postcondition
                        print(f"[set {set_index}] adopted {set_name} after "
                              f"ambiguous create", flush=True)
                    else:
                        save_state()                # keep the intent
                        print(f"ERROR: {ticker}: {exc}\n"
                              f"       The create is unresolved; refusing to "
                              f"start another upload. Re-run to reconcile.",
                              file=sys.stderr)
                        return EXIT_PARTIAL
                else:
                    save_state()                    # keep the intent
                    print(f"ERROR: {ticker}: {exc}\n"
                          f"       The upload is unresolved; refusing to start "
                          f"another one. Re-run to reconcile.", file=sys.stderr)
                    return EXIT_PARTIAL
            except RuntimeError as exc:
                # Non-retryable error for THIS sticker (e.g. bad image): the
                # request definitively did not apply, so the intent is settled
                # and the run may continue with the next item.
                if not placed and in_set == 0:
                    set_index -= 1  # undo the index reserved for the failed create
                state["in_flight"] = None
                failed.append(ticker)
                save_state()
                print(f"  skip {ticker}: {exc}", flush=True)
                continue
            in_set += 1
            uploaded += 1
            done.add(ticker)
            state["in_flight"] = None
            # Persist immediately: batching this every 10 items is what leaves a
            # window where an upload is live but unrecorded.
            save_state()
            if in_set >= args.per_set:
                in_set = 0
            notify_full_sets()  # send link as soon as a pack is full
            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(pending)} added this run", flush=True)
            time.sleep(0.1)
        # All logos processed: the last (partial) pack is finished too.
        notify_full_sets(final=True)
    finally:
        save_state()

    print("", flush=True)
    print(f"DONE. {len(done)} total emojis across {len(sets)} set(s). "
          f"New sets this run: {len(created)}. "
          f"Uploaded: {uploaded}. Failed/skipped: {len(failed)}.", flush=True)
    for s in sets:
        print(f"  https://t.me/addemoji/{s['name']}  ({s['count']})", flush=True)
    if failed:
        # A run that could not upload some images is not a success; automation
        # and the launcher menu previously saw exit 0 for a half-built pack.
        print(f"  failed/skipped: {', '.join(sorted(failed)[:20])}"
              f"{' ...' if len(failed) > 20 else ''}", file=sys.stderr)
    return ingest_exit_code(uploaded, len(failed))


if __name__ == "__main__":
    raise SystemExit(main())
