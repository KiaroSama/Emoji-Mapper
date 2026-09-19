# Phase 0 Research: Sandbox Isolation

Four questions had to be answered from this repository's own source before the
design could be fixed. All four are resolved; none needed an external source,
and none is left as NEEDS CLARIFICATION.

## Q1 — What acquires the source's writer claim, and what does it do when busy?

**Decision**: open a `Catalog` on the source and hold it for the whole clone.
Nothing else is needed.

**Rationale**: `emojikit/catalog.py:126` shows `Catalog.__init__` calling
`writer(self.path.parent).__enter__()` before it touches the database, and
`emojikit/maintenance.py:56` shows `writer()` is `_ownership(dir, "writer")`,
which does two things this feature would otherwise have to build:

- it takes `exclusive_lock(lock_path(directory))`, the native OS lease; and
- it raises `LockBusy` when `JOURNAL_NAME` exists in the directory, with a
  message naming the interrupted identity migration and how to resume it.

So "hold the source's writer lease across snapshot, state copy and media
rebasing" and "refuse safely when another writer exists or a migration is
pending" are both satisfied by keeping one `Catalog` object alive. Its `.db` is
also the live connection the snapshot needs, so the lease and the snapshot
source are the same object rather than two things to keep consistent.

**Alternatives considered**: calling `exclusive_lock` directly from the wrapper.
Rejected — it would take the lock without the migration-journal check, which is
the half that matters most, and it would duplicate a guard that already exists.

## Q2 — Does acquiring the native lock block?

**Decision**: no, and this is what makes the ownership test in FR-009 possible.

**Rationale**: `emojikit/packstate.py` takes the lock with
`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` on Windows and
`fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` on POSIX — both non-blocking,
raising `LockBusy` naming the holder.

This matters twice. A source being written by another process refuses
*immediately* rather than hanging a sandbox start-up. And the sweep can use
acquisition itself as the liveness test: a directory whose server still holds
its lease raises `LockBusy` and is skipped, while an abandoned one acquires and
is reclaimed. Without non-blocking acquisition the sweep would have to guess
liveness from file age, which is the check-then-act pattern
`exclusive_lock`'s own docstring records as the bug it replaced.

**Alternatives considered**: a PID file plus a liveness probe. Rejected — PIDs
are recycled, and the docstring documents a real interleaving where two
processes each believed they were alone under exactly that scheme.

## Q3 — What is the authoritative list of application-state files?

**Decision**: `emojikit/state_artifacts.py::state_files(data_dir)`.

**Rationale**: it already returns `publish_*.json` **and** `pack_plan.json`, and
it already raises on a symlinked or non-regular state path. Defect (5) —
`pack_plan.json` not copied — is therefore fixed by calling the existing
inventory instead of the wrapper's own `glob("publish_*.json")`. This is also
the list migration and rollback use, so the sandbox cannot drift from them: PR
#33 made that file the single shared inventory for exactly this reason.

**Alternatives considered**: extending the wrapper's glob to two patterns.
Rejected — it would be a second inventory, and the next state file added would
be missing from it silently.

## Q4 — How is the snapshot made WAL-inclusive, and with what budget?

**Decision**: `emojikit/sqlite_snapshot.py::backup(src.db, dest_con)` with the
default `BACKUP_TIMEOUT` of 30 s.

**Rationale**: `backup()` is SQLite's online backup API with its BUSY/LOCKED
retry loop bounded by a progress callback, which is precisely a consistent copy
including committed WAL content — `shutil.copy2` of `catalog.db` alone cannot
be, because the `-wal` file is a separate file it never copies.

The default 30 s budget is correct here and `RESTORE_TIMEOUT` (900 s) is not.
That asymmetry is deliberate and documented in the module: a restore writes onto
the LIVE catalog, where abandoning half-written is worse than waiting. A sandbox
capture writes into a throwaway directory, so failing fast and refusing to serve
is the safe outcome. Using the restore budget here would make a contended source
hang a sandbox for fifteen minutes.

**Alternatives considered**: `VACUUM INTO`. Rejected — it also produces a
consistent copy, but it has no bounded-retry wrapper in this project, so it
would reintroduce the unbounded wait that `sqlite_snapshot.py` exists to remove.

## Q5 — Where does the panel adopt an existing listener?

**Decision**: `emojikit/panel.py` calls `reopen_existing(...)` at two points
(around lines 572 and 625 on `main`'s current shape). A `reuse_existing=False`
parameter must bypass both, not just the first.

**Rationale**: the second call is the recovery path taken after a failed bind —
exactly the situation a sandbox hits when something else already holds its port.
Bypassing only the first would leave the defect reachable by the route most
likely to occur.

**Alternatives considered**: having the sandbox pick a random free port instead.
Rejected — the port being the real panel's + 1 is a deliberate invariant
(`DEFAULT_PORT = PANEL_PORT + 1`, imported, never retyped) so that a sandbox can
never take the real panel's port and a real panel is never mistaken for a
sandbox. Randomising it would trade one guarantee for another.

## Consolidated decisions

| # | Decision | Component |
|---|---|---|
| 1 | Source lease = one long-lived `Catalog` on the source | `emojikit/catalog.py` |
| 2 | Liveness test = non-blocking lock acquisition | `emojikit/packstate.py` |
| 3 | State inventory = `state_files()` | `emojikit/state_artifacts.py` |
| 4 | Snapshot = `backup()` at the default 30 s budget | `emojikit/sqlite_snapshot.py` |
| 5 | Reuse bypass covers BOTH `reopen_existing` call sites | `emojikit/panel.py` |

Nothing in this feature requires a new dependency, and the only genuinely new
artefact is the ownership marker described in [data-model.md](data-model.md).
