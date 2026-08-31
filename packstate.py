"""The pack state file: its shape, its atomic write, and its lock.

One writer per pack family, and never a half-written state file. Both
guarantees exist because both were violated in production: two publishers
raced on one family, and a crash mid-write left a state file that read back
as "nothing published yet" and re-uploaded the pack.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import subprocess
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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


class LockBusy(RuntimeError):
    """Another process already holds this pack family's lock."""


LOCK_DIR = ROOT / ".locks"
# How old a lock must be before a run whose holder is PROVABLY GONE may take it.
# Both conditions are required, and the liveness check is the one carrying the
# safety: a live holder is never stolen from at any age. This grace exists only
# to cover claiming being two steps -- O_CREAT|O_EXCL, then a separate write --
# because during that window the record is empty and its pid parses as 0, which
# reads as "dead". Seconds cover a window measured in microseconds.
#
# It was six hours, which had nothing left to protect once the liveness check
# was added, and which locked the owner out of resuming a publish they had just
# stopped themselves: the holder was dead, the work was half done, and the only
# way forward was to wait or to delete a lock file by hand.
LOCK_STALE_AFTER = 120


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
    recorded process is genuinely gone: age alone never condemns a lock, however
    long its holder has been running.

    Yields a ``heartbeat`` callable that refreshes the mtime. No caller uses it
    -- liveness, not age, is what protects a long run -- so do not build on it
    without checking that it is actually being called.
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
