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
  - GENERAL_BOT_TOKEN   -> @GodVerifyEmojiMapperbot, for general (non-coin) packs

Telegram limits each custom-emoji set to 200 emojis, so the images are split
into multiple sets named ``<base><n>_by_<botusername>``. Each emoji is given an
associated standard emoji (--emoji) and optional searchable keywords (from a
keywords.csv mapping ``ticker -> keywords``; falls back to the file name).

Usage (crypto coins, original bot):
  python build_pack.py --base gvcryptoemoji --title "@GodVerify Crypto Emoji" \
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
import re
import secrets
import subprocess
import sys
import tempfile
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


class StateInvalid(RuntimeError):
    """Resume state is not internally consistent and must not drive mutations."""


def validate_state_shape(state: dict, *, base: str, per_set: int) -> None:
    """Raise StateInvalid unless the resume state can be trusted.

    Valid JSON is not the same as valid state: a file can parse cleanly and
    still claim a negative sticker count or two sets sharing an index, and every
    later decision (which set is active, how many stickers to expect) is built
    on those numbers.
    """
    if not isinstance(state, dict):
        raise StateInvalid("state is not an object")
    if state.get("base") != base:
        raise StateInvalid(f"state belongs to base {state.get('base')!r}")

    done = state.get("done", [])
    if not isinstance(done, list) or not all(isinstance(x, str) for x in done):
        raise StateInvalid("'done' must be a list of item keys")

    sets = state.get("sets", [])
    if not isinstance(sets, list):
        raise StateInvalid("'sets' must be a list")
    seen_indexes: set[int] = set()
    last_index = 0
    for i, s in enumerate(sets):
        if not isinstance(s, dict):
            raise StateInvalid(f"sets[{i}] is not an object")
        name, index, count = s.get("name"), s.get("index"), s.get("count")
        if not isinstance(name, str) or not name:
            raise StateInvalid(f"sets[{i}] has no name")
        if not isinstance(index, int) or index < 1:
            raise StateInvalid(f"sets[{i}] has a bad index {index!r}")
        if index in seen_indexes:
            raise StateInvalid(f"sets[{i}] repeats index {index}")
        if index < last_index:
            raise StateInvalid(f"sets[{i}] index {index} goes backwards")
        if not isinstance(count, int) or not 0 <= count <= per_set:
            raise StateInvalid(
                f"sets[{i}] count {count!r} outside 0..{per_set}")
        seen_indexes.add(index)
        last_index = index

    intent = state.get("in_flight")
    if intent is not None and not isinstance(intent, (dict, str)):
        raise StateInvalid("'in_flight' must be an intent object or null")
    if isinstance(intent, dict):
        for field in ("key", "operation", "set_name"):
            if not intent.get(field):
                raise StateInvalid(f"in_flight is missing {field!r}")
        if intent["operation"] not in ("add", "create"):
            raise StateInvalid(
                f"in_flight operation {intent['operation']!r} is unknown")


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
    """Another process already holds this pack family's lock."""


LOCK_DIR = ROOT / ".locks"
LOCK_STALE_AFTER = 6 * 3600


def pack_family_lock_path(base: str) -> Path:
    """One lock per PACK FAMILY, shared by every tool that can mutate it.

    Locks used to be named after whichever state file a given tool happened to
    use -- coin_pack.lock, rebuild_dedup_state.json.lock, state_<base>.json.lock
    -- so a provider top-up and a rebuild could hold three different locks while
    mutating the same gvcryptoemoji* sets. Keying on the base name is what makes
    the exclusion real.
    """
    # Dots are dropped too: a base is [A-Za-z][A-Za-z0-9]* anyway, and keeping
    # them would let a hand-passed "../.." survive into the file name.
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_") or "default"
    return LOCK_DIR / f"pack_{safe}.lock"


def canonical_map_lock():
    """Serialise read-modify-write of coins/ticker_to_id.json.

    alias_map, enhance_map, remap_ids --apply, the providers, verify_logos and
    the rebuild mapping all rewrite the WHOLE file. Atomic replace stops a
    truncated file; it does not stop a lost update, where two writers each read
    the same map, apply different edits, and the second write silently discards
    the first. One lock around the whole read-modify-write does.
    """
    return exclusive_lock(LOCK_DIR / "canonical_map.lock")


def _lock_owner_is_alive(pid: int) -> bool:
    """Best-effort liveness check for the recorded lock holder.

    Returning True for every error made a crashed POSIX process look alive
    forever, so its lock could never be reclaimed. Distinguish the cases:
    "no such process" is a definite no, "not permitted" is a definite yes
    (the pid exists, it just is not ours), and anything genuinely unknown stays
    conservative.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return True          # cannot tell -> never steal
    try:
        os.kill(pid, 0)          # signal 0 only probes existence
        return True
    except ProcessLookupError:
        return False             # ESRCH: the holder is genuinely gone
    except PermissionError:
        return True              # EPERM: it exists under another user
    except OSError:
        return True              # unknown failure -> stay conservative


@contextlib.contextmanager
def exclusive_lock(path: Path, *, stale_after: float = LOCK_STALE_AFTER):
    """Exclusive lock so two runs cannot mutate one pack family at once.

    Ownership is explicit. The file records a unique token, and the holder
    removes the lock only if that token is still the one on disk -- previously a
    long but healthy run could have its lock "reclaimed" as stale by a second
    process, and would then delete the *replacement* holder's lock on the way
    out, leaving both free to mutate. A stale lock is only taken over when its
    recorded process is genuinely gone, and a long-running holder refreshes the
    mtime so age alone never condemns it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{secrets.token_hex(8)}"
    record = json.dumps({"token": token, "pid": os.getpid(),
                         "started": _utc_now()})

    def _claim() -> None:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, record.encode("utf-8"))
        finally:
            os.close(fd)

    try:
        _claim()
    except FileExistsError:
        held = {}
        stale_record = None       # the EXACT bytes judged stale, for the CAS below
        try:
            stale_record = path.read_text(encoding="utf-8")
            held = json.loads(stale_record)
        except (OSError, ValueError):
            pass
        age = 0.0
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            pass
        holder_pid = int(held.get("pid") or 0)
        if age < stale_after or _lock_owner_is_alive(holder_pid):
            # from None: the FileExistsError above is not a failure, it IS the
            # expected finding ("someone holds this"). Chaining it prints
            # "During handling of the above exception..." over a routine
            # outcome, which reads like a bug in the handler.
            raise LockBusy(
                f"{path.name} is held by pid {holder_pid or '?'} "
                f"(started {held.get('started', 'unknown')}, {age:.0f}s ago). "
                f"Refusing to mutate the same pack family concurrently.") from None
        print(f"  reclaiming lock {path.name}: pid {holder_pid} is gone "
              f"({age:.0f}s old)", flush=True)
        # Reclaiming is the one path where two processes can both decide to act:
        # they read the SAME stale record, and an unconditional unlink + create
        # let the second one delete the first one's freshly claimed lock and
        # take the family for itself -- two live holders, which is precisely
        # what this lock exists to prevent, arrived at through its recovery path.
        #
        # Three guards, because each closes a different order of events:
        #   1. delete only while the stale record we judged is still the one on
        #      disk, so a process that read the OLD record cannot remove a NEW
        #      holder's claim;
        #   2. O_EXCL still decides the winner if both delete before either
        #      creates -- the loser must back off, not claim on top;
        #   3. read our own token back, because neither check above is atomic
        #      with respect to the other process's whole sequence.
        try:
            if path.read_text(encoding="utf-8") == stale_record:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        try:
            _claim()
        except FileExistsError:
            raise LockBusy(
                f"{path.name} was reclaimed by another run while this one was "
                f"reclaiming it too. Refusing to mutate the same pack family "
                f"concurrently.") from None
        try:
            mine = path.read_text(encoding="utf-8") == record
        except OSError:
            mine = False
        if not mine:
            raise LockBusy(
                f"{path.name} was taken by another run immediately after this "
                f"one claimed it. Refusing to mutate the same pack family "
                f"concurrently.") from None

    def heartbeat() -> None:
        """Refresh the mtime so a healthy long run is never judged stale."""
        try:
            if path.read_text(encoding="utf-8") == record:
                os.utime(path, None)
        except OSError:
            pass

    try:
        yield heartbeat
    finally:
        # Remove the lock ONLY if we still own it. If another process reclaimed
        # it, deleting would hand a third process a free pass.
        try:
            if path.read_text(encoding="utf-8") == record:
                path.unlink(missing_ok=True)
        except OSError:
            pass


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


def worker_publish_url() -> str:
    """Where finished packs are announced from, when a Worker is deployed.

    Set ``WORKER_PUBLISH_URL`` (and ``WORKER_PUBLISH_SECRET``) to have the
    Cloudflare Worker post pack links to the channel instead of this process
    talking to Telegram directly. Unset means the old direct path, so an
    existing setup keeps working untouched.
    """
    return os.environ.get("WORKER_PUBLISH_URL", "").strip()


def announce_via_worker(packs: list[dict], *, note: str = "",
                        bot: str = "coin", style: str = "cards",
                        timeout: int = 30) -> None:
    """Ask the Worker to announce finished packs. Raises on any failure.

    ``packs`` is a list of ``{"name", "title", "count"}`` -- the Worker builds
    the message and owns the channel destination, so the link text lives in one
    place instead of being duplicated per publisher.

    NOT retried, for the same reason the direct send is not: the Worker's
    sendMessage is not idempotent and has no dedup key, so a timeout after
    Telegram accepted the post cannot be told from one before it, and retrying
    turns one outage into several identical announcements. The caller's
    ``state["sent"]`` guard is what makes a later re-run safe.
    """
    url = worker_publish_url()
    secret = os.environ.get("WORKER_PUBLISH_SECRET", "")
    if not url or not secret:
        raise RuntimeError("WORKER_PUBLISH_URL and WORKER_PUBLISH_SECRET must both be set")
    body = {"bot": bot, "packs": packs, "style": style}
    if note:
        body["note"] = note
    resp = requests.post(url, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {secret}"})
    if resp.status_code != 200:
        # The body can carry the Worker's reason; the bearer never appears in it.
        raise RuntimeError(f"worker announce failed (HTTP {resp.status_code}): "
                           f"{resp.text[:200]}")


def announce_packs(tg: "Telegram", owner_id: int, packs: list[dict], *,
                   bot: str, note: str = "", style: str = "cards") -> str:
    """Post finished packs' add-links. Returns where they went, for the log.

    ONE function, because this project had three publishers -- the single-pack
    build, the collector and the coin rebuild -- each carrying its own copy of
    "format the link and sendMessage". When the Worker arrived only the
    collector learned about it, so a coin rebuild or a plain build went on
    talking to Telegram from this machine while the owner believed the bot was
    posting. A third copy is how that happens again.

    Routing: the Worker when BOTH ``WORKER_PUBLISH_URL`` and
    ``WORKER_PUBLISH_SECRET`` are set -- one alone is a half-configured setup,
    and silently falling back would look identical to a working Worker. The
    direct path is unchanged otherwise.

    Not retried on either route (see ``announce_via_worker``). Callers keep
    their ``state["sent"]`` guard; this function has no memory.
    """
    if worker_publish_url() and os.environ.get("WORKER_PUBLISH_SECRET", "").strip():
        announce_via_worker(packs, bot=bot, note=note, style=style)
        return "the worker"
    dest = links_chat_id(owner_id)
    # Previews off: these messages are mostly addemoji URLs, and one preview
    # card per link buries them. The Worker route does the same by default.
    #
    # The direct path renders exactly what the Worker renders. If it did not,
    # switching the Worker on would silently change how a real post looks.
    if style == "list":
        lines = [note, ""] if note else []
        lines += [f"{p.get('title') or p['name']}. "
                  f"https://t.me/addemoji/{p['name']}" for p in packs]
        tg.send_message(dest, "\n".join(lines), disable_preview=True)
        return str(dest)
    if note:
        tg.send_message(dest, note, disable_preview=True)
    for p in packs:
        tg.send_message(dest, f"✅ {p.get('title') or p['name']}\n"
                              f"https://t.me/addemoji/{p['name']}",
                        disable_preview=True)
    return str(dest)


def _under_a_test_runner() -> bool:
    """True when a test runner, not a tool, owns this process.

    The suite's own guard (tests/__init__.py) sets EMOJI_MAPPER_NO_DOTENV, but
    it protects only what is imported AFTER it, and it runs at all only when
    `tests` is imported as a package -- `unittest discover -s tests` without
    `-t .` loads the modules as top level and skips it. Either way the modules
    under test call load_env() at import time and put the real credentials back;
    the scrub that follows removes credential-SHAPED names, so anything else in
    .env survives in os.environ for the whole run.

    Deciding here makes the protection independent of how the suite was invoked
    and of which module imported first. The signal is exact -- it reads the spec
    of the process entry point, so an ordinary CLI run cannot trip it -- and a
    false positive would only mean .env is not auto-loaded, with explicit
    environment variables still working. It fails safe in both directions.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    entry = getattr(spec, "name", "") or ""
    return entry.split(".")[0] in {"unittest", "pytest"} or "pytest" in sys.modules


def load_env() -> None:
    # Several modules call load_env() at IMPORT time, which would put the real
    # credentials straight back into os.environ after the suite scrubbed them --
    # reopening the hole that once let a test reach live Telegram and replace a
    # sticker in a production pack.
    if os.environ.get("EMOJI_MAPPER_NO_DOTENV") == "1" or _under_a_test_runner():
        return
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


def _usable_fuids(stickers: list) -> set[str] | None:
    """Distinct file_unique_ids of ``stickers``, or None if they cannot identify.

    Identity only works when EVERY sticker carries one and they are distinct.
    A missing id would collapse several stickers onto the same key, making a
    set look unchanged when it is not.
    """
    fuids = set()
    for s in stickers:
        fuid = s.get("file_unique_id")
        if not fuid:
            return None
        fuids.add(str(fuid))
    return fuids if len(fuids) == len(stickers) else None


class BotApiError(RuntimeError):
    """Telegram answered ok:false -- a definite rejection of THIS request.

    Distinct from a RuntimeError raised after the retries ran out, which means
    we never got an answer at all. That difference decides whether it is safe
    to send a replacement request: a rejection definitely did not apply, an
    unanswered request may have.
    """


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
                        raise BotApiError(f"{method} failed: {desc}")
                    wait = min(30 * attempt, 90, remaining)
                    print(f"  stickerset_invalid; name not released yet, "
                          f"wait {wait:.0f}s ({method})", flush=True)
                    time.sleep(wait)
                    continue
                raise BotApiError(f"{method} failed: {desc}")
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

    def _added_check(self, name: str, expected_before: int | None, *,
                     known_before: set[str] | None = None,
                     source: Path | None = None):
        """applied_check for addStickerToSet: did OUR sticker land?

        A count is not identity. "the set grew by one" is equally true when a
        second writer added something entirely different while our request
        failed -- and acting on that marks the wrong item done, which is how a
        ticker ends up pointing at another coin's artwork.

        When the caller captures the set's file_unique_ids beforehand
        (``known_before``) the check becomes identity-based: exactly one NEW
        identity appeared, so something really was added and we can attribute
        it. Two or more new identities means a concurrent writer was involved
        and attribution is unsafe -> UNKNOWN. Callers that cannot supply the
        snapshot fall back to the count, which is weaker and documented as such.
        """
        if expected_before is None:
            return None

        def check():
            # Remember that a live probe was needed. A failed attempt can let a
            # FOREIGN sticker in before our retry lands, so the set grows by two
            # while the caller counts one; ``_live_after_add`` re-reads the size
            # whenever this ran instead of assuming an increment.
            check.fired = True
            known, sset = self.probe_sticker_set(name)
            if not known or sset is None:
                return None  # unknown / set vanished: reconcile, don't guess
            stickers = sset.get("stickers", [])
            now = _usable_fuids(stickers)
            if known_before is None or now is None:
                # Without usable identities nothing here can be proved. A count
                # of expected+1 is NOT evidence -- it is equally produced by
                # someone else's sticker landing while ours failed -- and
                # answering False would re-send an upload that may have landed.
                return None
            new = now - known_before
            if not new:
                return False         # definitely nothing was added
            if len(new) > 1:
                return None          # someone else wrote too: unattributable
            if source is None:
                return None          # cannot prove the newcomer is ours
            # Exactly one new sticker. That is still not proof it is OURS: our
            # request may have failed while an external or manual add landed.
            # Compare its content with the image we sent.
            added = next((s for s in stickers
                          if str(s.get("file_unique_id")) in new), None)
            if added is None:
                return None
            same = self._sticker_matches(added, source)
            if same is True:
                return True
            if same is False:
                return False         # a foreign sticker landed; ours did not
            return None              # could not verify: reconcile, don't guess

        check.fired = False
        # Exposed so the post-add size read can require the set to actually
        # contain OUR sticker, rather than trusting a bare len(). Both halves
        # are needed there: the snapshot says which identities are new, the
        # source says which of them we sent.
        check.known_before = known_before
        check.source = source
        return check

    def _live_after_add(self, name: str, check) -> int | None:
        """The set's live size after an add, or None when +1 is sound.

        Only a RETRIED add can move the size by anything but one: the attempt
        that failed may have let a foreign sticker in (``_added_check`` answers
        False for exactly that, which re-sends ours), leaving the set two bigger
        while the caller counts one. Every later ``expected_before`` is derived
        from that number, so it has to describe the set rather than our own
        intentions. Probing only when the check actually ran keeps the cost at
        one extra round trip per network failure instead of one per sticker.
        """
        if check is None or not check.fired:
            return None
        known, sset = self.probe_sticker_set(name)
        # A bare len() is not a measurement of the set we just wrote to. Two
        # answers look like a number and are not one:
        #   * a read that has not caught up with our own acknowledged write
        #     reports one too few -- this client already assumes that lag
        #     elsewhere (it sleeps before probing), and booking the short count
        #     puts every later expected_before permanently out by one;
        #   * a MISSING set reports ZERO, which the caller reads as an empty set
        #     and answers by creating a second pack, sending its link, exiting 0.
        # Both are excluded by requiring the read to actually CONTAIN the
        # sticker we just added. Anything else is "no answer", which is what
        # AmbiguousUploadError already means here.
        if not known or sset is None:
            raise AmbiguousUploadError(
                f"addStickerToSet applied after a retry but {name} could not be "
                f"read back afterwards (unknown live state, or the set is gone)")
        stickers = sset.get("stickers", [])
        now = _usable_fuids(stickers)
        if now is not None and check.known_before is not None:
            new = now - check.known_before
            # "Something new is here" is not "ours is here", and a FOREIGN
            # sticker landing during the failed attempt is the exact case this
            # whole path exists for -- so a bare difference passed the guard in
            # precisely the situation it was written to catch, and the size read
            # off a set that may not hold our sticker at all was booked as the
            # result of our upload. Only the content answers it. This costs
            # nothing on the happy path: it runs only when a live probe was
            # already needed, and stops at the first match.
            if not any(self._sticker_matches(s, check.source) is True
                       for s in stickers
                       if str(s.get("file_unique_id")) in new):
                raise AmbiguousUploadError(
                    f"addStickerToSet reported success for {name} but no "
                    f"sticker in the set read back afterwards holds the image "
                    f"we sent; its size cannot be trusted")
        return len(stickers)

    def set_fuids(self, name: str) -> set[str] | None:
        """Usable identities of a set's stickers, or None when there are none.

        None means "identity is not available here" -- the caller must fall
        back to the weaker count check rather than conclude anything.
        """
        state, sset = self.probe_set_state(name)
        if state is not SetState.EXISTS:
            return None
        return _usable_fuids(sset.get("stickers", []))

    def _created_check(self, name: str, *, expect_first: Path | None = None):
        """applied_check for createNewStickerSet: did WE create this set?

        Mere existence is not proof. A set with the same name may already
        belong to someone else, or be left over from an earlier run, and
        adopting it after a transport failure silently attaches our state to a
        pack we did not build. When ``expect_first`` names the image we were
        creating the set with, the check also requires the live set to hold
        exactly one sticker whose content matches it.
        """
        def check():
            known, sset = self.probe_sticker_set(name)
            if not known:
                return None
            if sset is None:
                return False
            if expect_first is None:
                return True
            stickers = sset.get("stickers", [])
            if len(stickers) != 1:
                return None       # not the shape our create would have left
            return self._sticker_matches(stickers[0], expect_first)

        return check

    def _sticker_matches(self, sticker: dict, source: Path) -> bool | None:
        """Does a live sticker hold the image in ``source``?

        Telegram re-encodes on upload, so bytes never match; compare the
        decoded pixels through the project's own content key. Returns None when
        the comparison itself could not be made, so the caller keeps treating
        the outcome as unknown rather than as a negative.
        """
        try:
            from emojikit import media
        except Exception:         # noqa: BLE001 - media stack unavailable
            return None
        tmp = None
        try:
            fmt = media.telegram_sticker_format(sticker)
            tmp = Path(tempfile.gettempdir()) / f"_em_{sticker['file_unique_id']}"
            self.download_file(sticker["file_id"], tmp)
            return media.content_key(tmp, fmt) == media.content_key(source, fmt)
        except Exception:         # noqa: BLE001 - a failed probe is not a "no"
            return None
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: int | str, text: str, *,
                     disable_preview: bool = False) -> None:
        """Send a notification.

        ``disable_preview`` matters for a message that is mostly links: the coin
        family posts 30+ addemoji URLs and a preview card per link buries them.
        It used to be a private ``_call`` in the coin script for exactly that;
        it lives here so every announcer can ask for it.

        sendMessage is not idempotent and the Bot API offers no dedup key, so a
        network failure AFTER Telegram accepted the message cannot be told from
        one before -- a retry may post the link twice. Callers guard against
        repeats across runs (``state["sent"]``); within a run the exposure is
        bounded by retrying only once instead of the default five times, which
        keeps a genuine transient blip recoverable without turning one outage
        into five identical posts.
        """
        self._call("sendMessage", retries=2, data={
            "chat_id": chat_id, "text": text,
            "disable_web_page_preview": disable_preview,
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
            applied_check=self._created_check(name, expect_first=png))

    def add_sticker(self, user_id: int, name: str, png: Path,
                    emoji: str, keywords: str, *,
                    expected_before: int | None = None) -> int | None:
        """Add a static sticker. Pass ``expected_before`` (the live sticker
        count the caller expects BEFORE this add) to make network retries
        duplicate-proof; without it the historical blind retry is kept.

        The set's identities are snapshotted first so the applied-check can ask
        "did exactly one NEW sticker appear?" rather than "is the count one
        higher?", which a concurrent writer can satisfy while our own request
        failed.

        Returns the live sticker count when the add had to be retried, else
        None -- see ``_live_after_add``, which explains why the caller may not
        simply add one in that case."""
        before = self.set_fuids(name) if expected_before is not None else None
        check = self._added_check(name, expected_before,
                                  known_before=before, source=png)
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_sticker_json(emoji, keywords)),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=check)
        return self._live_after_add(name, check)

    # ----- multi-format helpers (static / animated / video) -------------- #
    def set_sticker_position(self, file_id: str, position: int) -> None:
        """Move an existing sticker to ``position`` (zero-based) in its set.

        The one mutation here that IS idempotent: setting the same sticker to
        the same index twice leaves the same set. So unlike addStickerToSet it
        can be retried normally -- no verified-retry machinery, no ambiguity to
        reconcile.

        Nothing is re-uploaded and nothing is recreated, so the sticker keeps
        its file_id AND its custom_emoji_id: anyone already using the emoji is
        unaffected by a reorder.
        """
        self._call("setStickerPositionInSet",
                   data={"sticker": file_id, "position": int(position)})

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
            applied_check=self._created_check(name, expect_first=path))

    def add_emoji(self, user_id: int, name: str, path: Path, fmt: str,
                  emoji_list: list[str], keywords: list[str], *,
                  expected_before: int | None = None) -> int | None:
        """Add one emoji (any format) to an existing custom-emoji set.

        ``expected_before`` (the live sticker count expected BEFORE this add)
        makes network retries duplicate-proof, and the return value reports the
        live size after a retry; see ``add_sticker``."""
        before = self.set_fuids(name) if expected_before is not None else None
        check = self._added_check(name, expected_before,
                                  known_before=before, source=path)
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_input_sticker(fmt, emoji_list, keywords)),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=check)
        return self._live_after_add(name, check)


_MIME_BY_EXT = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tgs": "application/gzip",
    ".webm": "video/webm",
}


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

    # Locked by pack FAMILY, not by state file: the coin providers and the
    # rebuild tool mutate the same sets through different state files, so a
    # per-file lock let them run concurrently against one family.
    try:
        with exclusive_lock(pack_family_lock_path(args.base)):
            return _run_build(args, token, state_file, sources, keywords)
    except LockBusy as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED


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
        try:
            validate_state_shape(loaded, base=args.base, per_set=args.per_set)
        except StateInvalid as exc:
            print(f"ERROR: resume state {state_file} is inconsistent: {exc}.\n"
                  f"       Every resume decision is derived from these numbers, "
                  f"so refusing to mutate Telegram from them.", file=sys.stderr)
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
    intent = state.get("in_flight")
    in_flight = _intent_key(intent)

    # 1) Settle the unresolved intent FIRST, against the set IT names.
    #    Probing only sets[-1] was wrong for an ambiguous CREATE of set #2 or
    #    later: the new set is not in state["sets"] yet, so its name lives only
    #    in the intent, and the old last set says nothing about whether it
    #    landed.
    if in_flight:
        if isinstance(intent, dict):
            target = intent.get("set_name")
            operation = intent.get("operation")
        elif sets:
            # A state file written before intents were structured records only
            # the item key. The last recorded set is the only target that
            # version could have been writing to, so reconcile against it
            # rather than refusing to upgrade.
            intent = {"key": in_flight, "operation": "add",
                      "set_name": sets[-1]["name"],
                      "expected_before": sets[-1]["count"]}
            target, operation = intent["set_name"], "add"
        else:
            target = operation = None
        if not target:
            print(f"ERROR: the unresolved upload of {in_flight} does not name a "
                  f"target set, so it cannot be reconciled.\n"
                  f"       Check Telegram, then delete {state_file.name} "
                  f"deliberately.", file=sys.stderr)
            return EXIT_FAILED

        set_state, sset = tg.probe_set_state(target)
        if set_state is SetState.UNKNOWN:
            print(f"ERROR: cannot determine the live state of {target}.\n"
                  f"       Refusing to continue while {in_flight} may be "
                  f"unresolved. Retry when Telegram is reachable.",
                  file=sys.stderr)
            return EXIT_PARTIAL

        # The image the interrupted request was carrying. Reconciliation
        # compares CONTENT against it: "the set exists" and "the count is one
        # higher" are both equally true when someone else's sticker landed and
        # ours did not.
        in_flight_src = next(
            (p for p in sources if p.stem.lower() == in_flight), None)

        recorded = next((s for s in sets if s["name"] == target), None)
        if operation == "create":
            if set_state is SetState.EXISTS:
                live_n = len(sset.get("stickers", []))
                # A create leaves EXACTLY one sticker. A set holding more was
                # not left by our interrupted create alone, so a matching first
                # sticker proves nothing about the rest -- adopting it records
                # someone else's stickers as this run's own.
                if live_n != 1:
                    print(f"ERROR: {target} holds {live_n} stickers, but the "
                          f"unresolved create of {in_flight} would have left "
                          f"exactly one.\n"
                          f"       Refusing to adopt a set this run may not have "
                          f"created.", file=sys.stderr)
                    return EXIT_FAILED
                first = (sset.get("stickers") or [None])[0]
                proof = (tg._sticker_matches(first, in_flight_src)
                         if first is not None and in_flight_src else None)
                if proof is not True:
                    print(f"ERROR: {target} exists but its first sticker "
                          f"{'does not match' if proof is False else 'could not be compared with'} "
                          f"{in_flight}.\n"
                          f"       Refusing to adopt a set this run may not have "
                          f"created.", file=sys.stderr)
                    return EXIT_FAILED if proof is False else EXIT_PARTIAL
                if recorded is None:
                    sets.append({"name": target,
                                 "title": intent.get("title", ""),
                                 "count": live_n,
                                 "index": intent.get("set_index",
                                                     len(sets) + 1)})
                else:
                    recorded["count"] = live_n
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                print(f"  adopted {target} created before the interruption "
                      f"({live_n} sticker(s))", flush=True)
            else:
                print(f"  create of {target} did not land; {in_flight} stays "
                      f"pending", flush=True)
        else:                                    # an ADD
            if set_state is SetState.MISSING:
                print(f"ERROR: {target} no longer exists, but an add to it is "
                      f"unresolved.\n       Reconcile manually before "
                      f"continuing.", file=sys.stderr)
                return EXIT_FAILED
            live_n = len(sset.get("stickers", []))
            expected = intent.get("expected_before")
            if expected is None and recorded is not None:
                expected = recorded["count"]
            if expected is None:
                print(f"ERROR: the unresolved add of {in_flight} to {target} "
                      f"records no expected count; refusing to guess.",
                      file=sys.stderr)
                return EXIT_FAILED
            if live_n < expected:
                print(f"ERROR: {target} holds {live_n} stickers but the "
                      f"unresolved add of {in_flight} expected at least "
                      f"{expected}.\n       Stickers were removed; refusing to "
                      f"guess which images are live.", file=sys.stderr)
                return EXIT_FAILED
            # Counting cannot answer this. The sequence this reconciler exists
            # for -- our attempt fails, a FOREIGN sticker lands, our retry then
            # succeeds -- leaves the set two bigger, and demanding expected or
            # expected+1 turned that into a permanent EXIT_FAILED with our
            # sticker live and off the books forever. Ask the only question that
            # matters instead: is OUR image among the ones that arrived?
            #
            # POSITION cannot bound that question. `expected` marks the tail
            # only while stickers are appended and never removed: delete one
            # and add ours and live_n == expected, which left the old slice
            # EMPTY, never consulted the content oracle, announced "did not
            # land" and re-uploaded an image that was already live -- exiting 0.
            # So the search space is the whole set, tail first: an ordinary
            # append is still found in one comparison, and the walk over the
            # rest costs downloads once per interrupted run, never once per
            # upload.
            stickers = sset.get("stickers", [])
            grew = live_n > expected
            # True: ours is live. False: provably absent. None: unproven.
            found = False
            for st in stickers[expected:] + stickers[:expected]:
                verdict = (tg._sticker_matches(st, in_flight_src)
                           if in_flight_src else None)
                if verdict is True:
                    found = True
                    break
                if verdict is None:
                    found = None         # keep looking; a match still decides it
            if found is True:
                # Ours is there. WHAT ELSE arrived does not change that, which
                # is the whole point: expected+2 is the normal outcome of a
                # foreign sticker landing between our failed attempt and our
                # successful retry, and rejecting it stranded a live sticker
                # off the books permanently.
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                if recorded is not None:
                    recorded["count"] = live_n
                print(f"  reconciled from live: {in_flight} was applied "
                      f"before the interruption", flush=True)
            elif found is False and not grew:
                print(f"  {in_flight} did not land; it stays pending", flush=True)
            elif found is False:
                # Ours is provably absent, yet the set grew: a hand edit, not
                # our upload. The counts every later position is derived from
                # are no longer ours to reason about.
                print(f"ERROR: {target} grew by {live_n - expected}, and none "
                      f"of its {live_n} stickers is {in_flight} -- someone else "
                      f"wrote to this set.\n       Refusing to attribute it to "
                      f"this run.", file=sys.stderr)
                return EXIT_FAILED
            else:
                print(f"ERROR: cannot verify whether {in_flight} is among the "
                      f"{live_n} sticker(s) in {target}.\n       Refusing to "
                      f"guess. Retry when the images can be compared.",
                      file=sys.stderr)
                return EXIT_PARTIAL
        state["in_flight"] = None                # verified postcondition

    # 2) Every RECORDED set must still match what state claims. A set that was
    #    deleted or shrunk by hand invalidates the counts the whole resume is
    #    built on, and drift < 0 used to be ignored entirely.
    for s in sets:
        set_state, sset = tg.probe_set_state(s["name"])
        if set_state is SetState.UNKNOWN:
            print(f"ERROR: cannot determine the live state of {s['name']}.\n"
                  f"       Retry when Telegram is reachable.", file=sys.stderr)
            return EXIT_PARTIAL
        if set_state is SetState.MISSING:
            print(f"ERROR: recorded set {s['name']} no longer exists.\n"
                  f"       Refusing to continue against a state that describes "
                  f"a deleted pack. Restore it, or delete {state_file.name} to "
                  f"start this family again.", file=sys.stderr)
            return EXIT_FAILED
        live_n = len(sset.get("stickers", []))
        if live_n < s["count"]:
            print(f"ERROR: {s['name']} holds {live_n} stickers but state records "
                  f"{s['count']}; stickers were removed.\n"
                  f"       Refusing to publish against a shrunken set.",
                  file=sys.stderr)
            return EXIT_FAILED
        if live_n > s["count"]:
            print(f"ERROR: {s['name']} holds {live_n} stickers but state records "
                  f"{s['count']} and no in-flight upload explains it.\n"
                  f"       Refusing to guess which images are already live.",
                  file=sys.stderr)
            return EXIT_FAILED

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
            dest = announce_packs(tg, args.user_id,
                                  [{"name": name, "title": title}], bot="general")
            sent.add(name)
            print(f"  sent link for {name} to {dest}", flush=True)
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
                        live = tg.add_sticker(args.user_id, set_name, path,
                                              args.emoji, kw,
                                              expected_before=in_set)
                        # A retried add reports what is actually live: the failed
                        # attempt can have let a foreign sticker in, so the set
                        # grew by two while this counted one. Assuming +1 there
                        # made every later expected_before wrong by one.
                        sets[-1]["count"] = (sets[-1]["count"] + 1 if live is None
                                             else live)
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
                    # Replace the intent before the CREATE. On the rollover path
                    # the persisted intent still describes the ADD to the FULL
                    # set, so a crash here would send a restart looking at the
                    # wrong set and conclude the create never happened.
                    state["in_flight"] = make_intent(
                        key=ticker, operation="create", set_name=set_name,
                        set_index=set_index, expected_before=0, title=title)
                    save_state()
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
                    stickers = (sset or {}).get("stickers", [])
                    # Shape is not identity -- the same lesson the restart branch
                    # already learned. A set of this name holding one sticker may
                    # be a stranger's; adopting it on the count records a foreign
                    # pack as ours, marks this item done though it was never
                    # uploaded, publishes the link, and then keeps writing our
                    # stickers into someone else's set. Our create puts our image
                    # in first, so that is what has to answer.
                    ours = (tg._sticker_matches(stickers[0], path)
                            if set_state is SetState.EXISTS and len(stickers) == 1
                            else None)
                    if ours is True:
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
            # Follow the recorded set rather than incrementing separately: after
            # a retried add that number is the live size, and two counters that
            # advance independently drift apart exactly when it matters.
            in_set = sets[-1]["count"]
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
