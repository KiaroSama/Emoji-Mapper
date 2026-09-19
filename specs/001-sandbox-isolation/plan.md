# Implementation Plan: Sandbox Isolation

**Branch**: `001-sandbox-isolation` | **Date**: 2026-09-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-sandbox-isolation/spec.md`

## Summary

`scripts/panel_sandbox.py` clones the catalog and still leaves four paths back to
the owner's real data (a forwarded `--data-dir`, hard-linked media, external
media paths left unrewritten, a WAL-blind file copy) plus one path to destroying
a live sandbox (a prefix-only sweep) and one to serving an unidentified listener.

The approach is almost entirely assembly, not invention: every mechanism the
spec asks for already exists in this repository and is already tested.

| Requirement | Existing component |
|---|---|
| FR-002 source writer claim | `Catalog.__init__` acquires `writer(path.parent)`; holding the object holds the claim |
| FR-003 WAL-inclusive snapshot | `emojikit/sqlite_snapshot.py::backup(src_conn, dst_conn, timeout=)` |
| FR-006 state inventory | `emojikit/state_artifacts.py::state_files(data_dir)` — already returns `publish_*.json` **and** `pack_plan.json`, and already refuses symlinks |
| FR-009/010 native lease | `emojikit/packstate.py::exclusive_lock` — an OS lock (`flock` / `msvcrt`) whose file is deliberately never unlinked |

So the clone reduces to: open a `Catalog` on the SOURCE (that is the lease), use
its live connection as the backup source (that is the WAL fix), walk
`state_files()` (that is the plan fix), and copy media per row by content key
(that is the link and archive fix). The one genuinely new artefact is the
ownership marker that makes the sweep safe.

## Technical Context

**Language/Version**: Python 3.11 (CI also runs 3.12)

**Primary Dependencies**: standard library only for this feature — `sqlite3`,
`shutil`, `argparse`, `json`, `tempfile`, `os`. In-repo: `emojikit.catalog`,
`emojikit.sqlite_snapshot`, `emojikit.state_artifacts`, `emojikit.packstate`,
`emojikit.panel`.

**Storage**: SQLite catalog plus media files on disk; JSON application state.

**Testing**: standard-library `unittest`, started as
`python -m unittest discover -s tests -t . -p "test_*.py"`.

**Target Platform**: Windows-first; CI also runs Linux. Both POSIX `flock` and
Windows byte-range locking are already handled inside `exclusive_lock`.

**Project Type**: single project — a CLI wrapper around a local web panel.

**Performance Goals**: sandbox start-up stays in the low seconds for the current
collection (about 1000 media files). Copying instead of hard-linking is the
deliberate cost; the audit's own figure is roughly 20 MB per 200 emoji.

**Constraints**: cooperative test-data isolation, not an OS boundary. The clone
must be refused rather than completed whenever its fidelity cannot be proved.

**Scale/Scope**: one wrapper script, one small entry-point change in
`emojikit/panel.py`, one test module.

## Constitution Check

*GATE: passed before Phase 0, re-checked after Phase 1 design — see below.*

| Principle | Assessment |
|---|---|
| **I. Unknown Is Not False** | PASS, and the feature is largely an application of it. A directory whose owner cannot be determined is not abandoned (FR-009); an option that cannot be recognised is not harmless (FR-001); a media file that cannot be read is not copyable (FR-007 aborts rather than leaving a production reference). |
| **II. Identity Is Content, Never Position** | PASS. Media is copied into a destination named by content key, so the clone's layout carries no positional assumption, and `file_path` rewriting is keyed by `content_key`. |
| **III. The Owner's Packs Are Live Data** | PASS — the feature exists to enforce it. |
| **IV. The Suite Can Never Reach Telegram** | PASS. FR-012 is already implemented and tested; this plan preserves it and adds the data half of the same boundary. |
| **V. Intent And Execution Are Different Surfaces** | PASS. FR-006 copies `pack_plan.json`, so the sandbox can show saved intent instead of inferring it from live membership. |
| **800-line ceiling** | WATCH. `scripts/panel_sandbox.py` is ~170 lines today and this roughly doubles it — still far under. The clone logic goes in its own module rather than the wrapper if it would push the wrapper past ~400, so the CLI stays readable. |
| **CI First** | PASS. The new suite is plain `unittest`, discovered automatically by the `build` jobs. It is NOT marked `RUNS_ON_NATIVE_WINDOWS` unless a test genuinely needs native locking semantics; those that do carry the marker and the `windows-safety` job list is updated in the same change, because `tests/test_ci_coverage.py` enforces the match in both directions. |

No violations. Complexity Tracking is therefore omitted.

## Project Structure

### Documentation (this feature)

```text
specs/001-sandbox-isolation/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── cli.md           # The wrapper's command-line contract
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit-tasks)
```

### Source Code (repository root)

```text
emojikit/
├── catalog.py           # unchanged — its constructor IS the source lease
├── sqlite_snapshot.py   # unchanged — backup() is the WAL-inclusive snapshot
├── state_artifacts.py   # unchanged — state_files() is the inventory
├── packstate.py         # unchanged — exclusive_lock() is the native lease
├── panel.py             # CHANGED: main(argv=None, *, reuse_existing=True)
└── sandbox_clone.py     # NEW: clone_catalog, the ownership marker, the sweep

scripts/
└── panel_sandbox.py     # CHANGED: allowlist parsing, in-process launch

tests/
└── test_panel_sandbox.py  # EXTENDED: the credential tests already here, plus
                           # clone fidelity, refusal and sweep-ownership tests
```

**Structure Decision**: the clone/sweep logic moves into `emojikit/sandbox_clone.py`
and the wrapper keeps only argument handling and the launch. Two reasons, both
concrete: the logic needs importing by tests without importing a CLI, and the
file-size rule wants a new file named for a responsibility rather than a wrapper
grown to twice its size. `scripts/panel_sandbox.py` stays the entry point.

## Post-Design Constitution Re-Check

Re-checked after Phase 1. Still no violations. One design decision worth
recording because it looks like a violation and is not: `clone_catalog` copies
every media file rather than hard-linking, which is strictly more expensive and
would normally be the kind of cost a lazy design avoids. It is required by
FR-004 — a hard link makes the clone's bytes the source's bytes, which is the
defect, not an optimisation of it.

## Complexity Tracking

Not applicable: the Constitution Check passed with no violations.
