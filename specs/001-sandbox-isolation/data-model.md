# Phase 1 Data Model: Sandbox Isolation

## Terms

Three documents used three words for two things. Fixed here, and these are the
words the code and its comments use:

- **lock** — the operating-system lock itself (`exclusive_lock`: `flock` on
  POSIX, a byte-range lock on Windows). Non-blocking; raises `LockBusy`.
- **lease** — `writer(data_dir)`, which takes that lock AND refuses when a
  migration journal is present. What `Catalog` acquires in its constructor.
- "claim" is not used.


Three things have shape in this feature. Only the third is new.

## 1. Source (read-only, claimed)

The owner's real catalog directory, normally `collection/`.

| Element | Meaning here |
|---|---|
| `catalog.db` (+ `-wal`, `-shm`) | read through a live connection, never copied as bytes |
| `.lock` (whatever `lock_path()` names) | the native claim the clone holds for its duration |
| `<JOURNAL_NAME>` | if present, a migration is interrupted and the clone REFUSES |
| `publish_*.json`, `pack_plan.json` | enumerated by `state_files()`, copied out |
| media files | referenced by `items.file_path`; may live inside the directory, elsewhere on disk (the owner's archive), or at a project-relative path |

**Invariant**: after any clone attempt, success or failure, the source's
database, state files and media are byte-identical. Lock-diagnostic records
written by normal claim acquisition are the one permitted change.

## 2. Destination (the clone)

A fresh directory under the system temp area, named `panel-sandbox-<random>`.

```text
panel-sandbox-ab12cd34/
├── catalog.db           # online snapshot, WAL content included
├── publish_*.json       # copied via state_files()
├── pack_plan.json       # copied via state_files() -- the saved curation intent
├── media/
│   └── <content_key>.<ext>   # one file per catalog row, independent bytes
└── .sandbox-owner.json  # the marker below
```

**Media naming is by content key, not by the source's layout.** Two reasons:
a row whose source file lived outside the source tree has no relative path to
preserve, and content key is this project's identity everywhere else
(Constitution II). A row's `file_path` is rewritten to its copy.

**Invariants**:

- every `items.file_path` resolves inside the destination;
- no destination file shares storage with a source file (no hard links);
- the row count equals the source's at snapshot time.

## 3. Ownership marker (new)

`.sandbox-owner.json` inside each sandbox directory:

```json
{
  "version": 1,
  "directory": "<absolute path of the directory this marker describes>",
  "created_utc": "2026-09-19T14:31:07Z",
  "pid": 12345
}
```

`directory` is what makes the marker **directory-bound**: a marker copied or
moved into another directory describes a path that is not the one it now sits
in, and is therefore foreign. `pid` is diagnostic only and decides nothing --
PIDs are recycled, which is why liveness is proved by the lock, not by this file.

### Reclaim decision table

A directory is reclaimed only when BOTH columns say yes.

| Directory state | Marker valid and self-describing? | Lease acquired? | Action |
|---|---|---|---|
| served by a running sandbox | yes | no (`LockBusy`) | leave |
| abandoned by a killed sandbox | yes | yes | **remove** |
| no marker | no | n/a | leave |
| marker naming another directory | no | n/a | leave |
| marker unreadable or wrong schema | no | n/a | leave |
| symbolic link | n/a | n/a | leave, never follow |

**The lock file itself is retained in every case**, including a reclaim: on
POSIX, unlinking the inode a live holder locked makes that claim invisible to
the next run, which is the hole `exclusive_lock`'s docstring records. A small
tombstone costs nothing.

**Unknown is not abandoned.** Every "no" above is a refusal to delete, never a
default to delete -- Constitution I applied to a filesystem.
