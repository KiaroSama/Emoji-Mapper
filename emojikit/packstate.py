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
import stat
import subprocess
import os
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
# Kept only so callers and tests that pass it keep working. Ownership is no
# longer decided by age or by a liveness probe -- see exclusive_lock -- so this
# number governs nothing. It survives as the documented default for the one
# thing age is still good for: telling a user how long the holder has been there.
LOCK_STALE_AFTER = 120

# The lock is taken on a byte far past any metadata this file will ever hold.
# Windows byte-range locks are MANDATORY: locking byte 0 would make the record
# unreadable to the very process that needs to name the holder in its error
# message. Locking beyond the written region leaves the record readable to
# everyone and still gives us a unique byte to contend for. Locking a range
# past EOF is legal and is the standard way to do this.
_LOCK_BYTE = 4096
# Fixed width, space padded, always written at offset 0. A short record written
# over a longer one would otherwise leave the tail of the old one behind and
# the JSON would not parse.
_RECORD_WIDTH = 512

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def pack_family_lock_path(base: str) -> Path:
    """One lock per PACK FAMILY, shared by every tool that can mutate it.

    Locks used to be named after whichever state file a given tool happened to
    use -- coin_pack.lock, rebuild_dedup_state.json.lock, state_<base>.json.lock
    -- so a provider top-up and a rebuild could hold three different locks while
    mutating the same cryptoemoji* sets. Keying on the base name is what makes
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

    THE OPERATING SYSTEM owns the exclusion, for the whole critical section:
    ``flock`` on POSIX, a byte-range lock through ``msvcrt`` on Windows. The
    lock file itself is only a stable target to contend for, plus a place to
    record who is holding it; its CONTENTS confer nothing.

    That is a deliberate replacement of a check-read-unlink protocol, which
    could admit two publishers at once through its own recovery path. The
    interleaving, proved with two real processes: A reads the dead holder's
    record, judges it stale and pauses just before the unlink; B reads the same
    record, reclaims, verifies its own token and enters; A resumes, unlinks
    B's LIVE claim, creates its own, reads its own token back -- and enters
    too. Every guard there was a compare-then-act on a file two processes can
    change between the compare and the act, so adding a fourth token check
    could never have closed it. An OS lock has no such window.

    Consequences worth knowing:

    * **The file is never deleted.** Unlinking is what would reintroduce the
      hole: two processes can each hold a lock on a different inode at the same
      path and both believe they are alone. A leftover lock file is not a held
      lock, and costs nothing.
    * **A crash releases the lock**, because the OS drops it when the process
      exits and its handle closes. There is nothing to reclaim and no age to
      judge, which is why ``stale_after`` no longer decides anything.
    * **Not reentrant.** A second acquisition in the same process opens a second
      handle and the OS refuses it, exactly as it refuses another process. The
      project-wide lock ORDER (pack family first, then the canonical map)
      therefore still matters; see `tests/test_lock_order.py`.
    * **Unknown failure refuses.** If the lock cannot be taken for a reason
      that is not "someone holds it" -- a filesystem with no locking, a
      permission error -- this raises ``LockBusy`` rather than proceeding.
      Never granting ownership is the conservative answer.

    Yields a ``heartbeat`` callable that refreshes the mtime. No caller uses it;
    it exists so a human reading the file can see how long the holder has been
    there, and it no longer has any bearing on ownership.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{secrets.token_hex(8)}"
    record = json.dumps({"token": token, "pid": os.getpid(),
                         "started": _utc_now()})

    # O_CREAT without O_EXCL, and never O_TRUNC: the file may already exist,
    # possibly held by someone else, and truncating it would destroy the record
    # naming the holder we are about to report.
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        _take_os_lock(fd, path)
    except BaseException:
        os.close(fd)
        raise

    try:
        # Only now, holding the lock, does the record become ours to write.
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, record.ljust(_RECORD_WIDTH).encode("utf-8"))

        def heartbeat() -> None:
            """Refresh the mtime, for a human reading `ls -l` on .locks/."""
            try:
                os.utime(path, None)
            except OSError:
                pass

        yield heartbeat
    finally:
        # Release explicitly rather than relying on close: on Windows the two
        # are not the same call, and an unreleased range on a reused handle is
        # a hang waiting to happen.
        try:
            _drop_os_lock(fd)
        finally:
            os.close(fd)


def _take_os_lock(fd: int, path: Path) -> None:
    """Take the OS lock, or raise LockBusy naming whoever has it."""
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise LockBusy(_busy_message(path, exc)) from None


def _drop_os_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass          # closing the handle releases it anyway


def _busy_message(path: Path, exc: OSError) -> str:
    """Name the holder from the record, which is a HINT and not the authority.

    The OS says someone holds the lock; the record only says who wrote it last.
    Those can disagree two ways -- a holder that crashed between taking the lock
    and writing its record leaves the PREVIOUS run's text behind, and a holder
    that finishes while this message is being built was real a moment ago -- so
    the liveness probe is reported as the observation it is and never turned
    into a claim about which of those happened.
    """
    held: dict = {}
    try:
        held = json.loads(path.read_text(encoding="utf-8").strip() or "{}")
    except (OSError, ValueError):
        pass
    pid = 0
    try:
        pid = int(held.get("pid") or 0)
    except (TypeError, ValueError):
        pass
    age = ""
    try:
        age = f", {time.time() - path.stat().st_mtime:.0f}s ago"
    except OSError:
        pass

    if pid and _lock_owner_is_alive(pid):
        who = f"pid {pid} (started {held.get('started', 'unknown')}{age})"
    elif pid:
        who = (f"another run (the lock file names pid {pid}, which was not "
               f"running when this was checked -- it either finished just now "
               f"or never wrote its own record)")
    else:
        who = "another run"
    return (f"{path.name} is held by {who}. "
            f"Refusing to mutate the same pack family concurrently. [{exc.strerror or exc}]")


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_REPLACE_DEADLINE = 1.0   # seconds; pip bounds the same retry at 1 s


def _replace(src: Path, dst: Path) -> None:
    """``os.replace``, with a bounded retry for Windows' transient refusal.

    Windows answers ``PermissionError`` when the destination is itself mid-way
    through another replace, or is held open without delete sharing (a second
    writer, a virus scan). Measured on this machine: two writers to one file,
    released together, lost one write in 13 of 40 runs. POSIX ``rename`` never
    refuses that way, so only Windows retries, and only that error. pip does the
    same: ``src/pip/_internal/utils/filesystem.py``,
    ``replace = retry(stop_after_delay=1, wait=0.25)(os.replace)``.

    The wait is a short, growing backoff inside a hard deadline, not a guess at
    readiness: Windows gives no signal when the other rename completes.
    """
    if os.name != "nt":
        os.replace(src, dst)
        return
    deadline = time.monotonic() + _REPLACE_DEADLINE
    delay = 0.005
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def write_json_atomic(path: Path, data) -> None:
    """Write JSON so an interrupted run can never leave a truncated file.

    Upload progress lives in these files: a half-written state file is read back
    as corrupt (or, worse, silently replaced by an empty default) and the whole
    source set gets uploaded again. Write to a sibling temp file, flush it to
    disk, then rename -- rename is atomic on both NTFS and POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An exclusive, invocation-owned sibling avoids truncating someone else's
    # .tmp file or sharing an inode with another atomic writer. Callers still
    # need their domain lock around an entire read-modify-write transaction.
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates its file 0600 and os.replace publishes that inode, so
        # without this every rewrite narrowed an existing 0644 file to owner-only.
        # A NEW file keeps 0600: the umask default would need os.umask(), which is
        # process-global and races other threads.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        _replace(tmp, path)
    finally:
        # After replace the pathname is gone; on failure only our inode is removed.
        tmp.unlink(missing_ok=True)
