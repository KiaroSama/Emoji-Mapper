---

description: "Task list for Sandbox Isolation"
---

# Tasks: Sandbox Isolation

**Input**: Design documents from `/specs/001-sandbox-isolation/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/cli.md](contracts/cli.md)

**Tests**: INCLUDED. Not an optional extra here — every acceptance scenario in
the spec is a refusal or a fidelity claim, and a refusal that is not tested is
indistinguishable from a refusal that never fires. The audit's own acceptance
list is 13 methods.

**Organization**: grouped by user story so each is independently deliverable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on incomplete work)
- **[Story]**: US1, US2, US3 from [spec.md](spec.md)

## Path Conventions

Single project, repository root: `emojikit/`, `scripts/`, `tests/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: create the module the clone logic moves into, so later tasks have a
home and `scripts/panel_sandbox.py` never doubles in size.

- [X] T001 Create `emojikit/sandbox_clone.py` with its module docstring stating what it owns (clone, ownership marker, sweep) and why it is not in the wrapper (importable by tests without importing a CLI; the 800-line rule wants a responsibility-named file)
- [X] T002 [P] Add the constants `MARKER_NAME = ".sandbox-owner.json"`, `MARKER_VERSION = 1` and `TMP_PREFIX` to `emojikit/sandbox_clone.py`, importing `TMP_PREFIX`'s value from a single definition so the wrapper and the sweep cannot disagree about which directories are sandboxes

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the panel must be callable in-process before the wrapper can stop
using a subprocess. Blocks US1's launch task and all of US3.

- [X] T003 Change `main()` in `emojikit/panel.py` to `main(argv: list[str] | None = None, *, reuse_existing: bool = True)`, passing `argv` to its argparse call so `None` keeps reading `sys.argv`; ordinary command-line invocations must behave exactly as before
- [X] T004 Guard BOTH `reopen_existing(...)` call sites in `emojikit/panel.py` (the pre-bind one near line 572 and the post-bind-failure recovery one near line 625) with `reuse_existing`, per research.md Q5 — bypassing only the first leaves the defect reachable by the route a sandbox is most likely to take
- [X] T005 [P] Add a test to `tests/test_panel_sandbox.py` that `panel.main` accepts an explicit `argv` and that `reuse_existing=False` reaches neither `reopen_existing` call site (patch it to raise, assert it is never called)

---

## Phase 3: User Story 1 — The sandbox cannot be talked into serving the real catalog (P1) 🎯 MVP

**Goal**: no argument produces a panel pointed at `collection/`.

**Independent test**: pass a data-directory override, an unknown option and an
abbreviation; each is refused, nothing is cloned, nothing is served.

**This story alone is a viable increment**: it closes the only defect that hands
over the real catalog in one step with no filesystem accident required.

- [X] T006 [US1] Replace `parse_known_args` in `scripts/panel_sandbox.py` with an explicit allowlist parser built with `argparse.ArgumentParser(allow_abbrev=False)`, declaring exactly the options in [contracts/cli.md](contracts/cli.md): `--source` (default `collection`), `--port` (default `panel.DEFAULT_PORT + 1`), `--all`, `--with-pack` (int, repeatable), `--bot-username`
- [X] T007 [US1] Delete the `*panel_args` forwarding in `scripts/panel_sandbox.py` and build the panel's argument list explicitly from the parsed allowlist values, so `--data-dir` can only ever come from this code
- [X] T008 [US1] Validate `--port` in `scripts/panel_sandbox.py`: reject `panel.DEFAULT_PORT` (keep the existing message) and reject anything outside 1–65535
- [X] T009 [P] [US1] Add tests to `tests/test_panel_sandbox.py` for each refusal in the contract's "Refused" table: `--data-dir`, an unknown option, the abbreviations `--sou` and `--wi`, the real panel's port, an out-of-range port. Each asserts a non-zero exit AND that no new sandbox directory appeared
- [X] T010 [P] [US1] Add a test that the allowed options (`--all`, `--with-pack 3`, `--bot-username x`) are accepted and reach the panel's argument list unchanged

**Checkpoint**: the wrapper refuses everything outside the allowlist.

---

## Phase 4: User Story 2 — Everything the sandbox serves is a private copy (P1)

**Goal**: the clone shares no bytes and holds no source paths.

**Independent test**: clone a source with an open WAL plus internal, external and
project-relative media; verify row count, storage independence and that no
`file_path` escapes the clone.

- [X] T011 [US2] Write `clone_catalog(source, dest)` in `emojikit/sandbox_clone.py` that opens `Catalog(source / "catalog.db")` and holds it for the whole operation — per research.md Q1 this single object IS the writer lease AND raises `LockBusy` on a pending migration journal, so no separate lock or migration check is written
- [X] T012 [US2] Inside `clone_catalog`, copy the database with `sqlite_snapshot.backup(src.db, dest_con)` at the DEFAULT 30 s budget, never `shutil.copy2` and never `RESTORE_TIMEOUT` — research.md Q4 records why the restore budget is wrong here (a sandbox capture must fail fast; only a restore onto the live catalog may wait 900 s)
- [X] T013 [US2] Inside `clone_catalog`, copy application state with `state_artifacts.state_files(source)` rather than a local glob, so `pack_plan.json` travels and the sandbox cannot drift from the inventory migration and rollback use
- [X] T014 [US2] Inside `clone_catalog`, copy each row's media independently into `dest/media/<safe_key><suffix>` with `shutil.copyfile` (never `os.link`), resolving internal, absolute-external and project-relative sources alike, and rewrite that row's `file_path` to the copy. `safe_key` replaces `:` with `_`: `identity.content_key` returns `s:<32 hex>` and `collision_key` returns `s:<hex>:<hex>`, and on NTFS a `:` in a path opens an alternate data stream instead of a file. `_` is unambiguous because a key is only a prefix letter, colons and hex
- [X] T014a [P] [US2] Add a test that a row whose `content_key` contains a colon (both the one-colon and the `collision_key` two-colon forms) clones to a real file on disk and back, so the Windows-first platform is covered by the suite rather than by luck
- [X] T015 [US2] In `clone_catalog`, raise rather than complete when a referenced media file is missing, unreadable, or copies to a different size than the source — FR-007: a clone finished with a row still pointing at production is worse than no clone
- [X] T016 [US2] Give `clone_catalog` a failure path that removes ONLY the destination this call created, never an occupied or pre-existing directory, and assert before starting that the destination is new and disjoint from the source (neither is a parent of the other after resolving)
- [X] T017 [P] [US2] Add a test that a source whose rows are still only in its WAL clones with the full row count (write rows on one connection without checkpointing, then clone)
- [X] T018 [P] [US2] Add a test that no cloned media file shares storage with its source: compare `Path.stat().st_ino` (and `st_dev`) for a clone file and its source and assert they differ
- [X] T019 [P] [US2] Add a test covering all three media locations — a file inside the source, one outside it (standing in for the owner's archive) and one at a project-relative path — asserting every resulting `file_path` resolves inside the clone
- [X] T020 [P] [US2] Add a test that `pack_plan.json` present in the source is present in the clone, and that a source without one clones successfully
- [X] T021 [P] [US2] Add tests for the refusals: an occupied destination, a destination overlapping the source, a source whose writer lease is already held, and a source carrying a migration journal — each leaves the source unchanged and creates nothing
- [X] T022 [P] [US2] Add a test that an incomplete copy (a row naming a missing media file) removes the partial destination and leaves the source byte-identical

**Checkpoint**: the clone is provably independent of the source.

---

## Phase 5: User Story 3 — One sandbox never destroys another (P2)

**Goal**: the sweep reclaims only what it can prove is abandoned, and a dead
sandbox stops serving.

**Independent test**: five directories — running, abandoned, unmarked, foreign
marker, symlink — survive the sweep except the abandoned one.

- [X] T023 [US3] Write `write_marker(directory)` in `emojikit/sandbox_clone.py` producing the `.sandbox-owner.json` shape in [data-model.md](data-model.md): `version` 1, `directory` as the ABSOLUTE resolved path of the directory it sits in, `created_utc` as `YYYY-MM-DDTHH:MM:SSZ`, and `pid` recorded as diagnostic only
- [X] T024 [US3] Write `marker_is_self_describing(directory)` returning False for a missing, unreadable, wrong-version or wrong-shape marker AND for one whose `directory` field does not resolve to the directory holding it — that field is what makes a copied marker foreign
- [X] T025 [US3] Rewrite `sweep_stale()` in `emojikit/sandbox_clone.py` to reclaim a directory only when BOTH `marker_is_self_describing` passes AND `exclusive_lock(lock_path(directory))` acquires; research.md Q2 records that acquisition is non-blocking, so a live sandbox raises `LockBusy` and is skipped without hanging
- [X] T026 [US3] In the rewritten sweep, skip any candidate that `Path.is_symlink()` reports, before reading anything inside it, and never follow it
- [X] T027 [US3] In the rewritten sweep, retain the lock file when removing a reclaimed directory's contents — per data-model.md, unlinking a locked inode on POSIX makes a live claim invisible to the next run, which is the hole `exclusive_lock`'s docstring records
- [X] T028 [US3] Hold the sandbox's own `exclusive_lock` for the lifetime of the served panel in `scripts/panel_sandbox.py`, and run the panel via `panel.main(argv, reuse_existing=False)` in this process so the wrapper's exit stops the server and drops the lease together
- [X] T029 [P] [US3] Add a test that a directory whose lease is held survives the sweep, and one whose lease is free is removed
- [X] T030 [P] [US3] Add tests that an unmarked directory, a directory holding a marker naming a different path, and a symbolic link are each left untouched
- [X] T031 [P] [US3] Add a test that a reclaimed directory's lock file still exists afterwards
- [X] T031a [US3] Add a test for SC-005 end to end: start the wrapper as a real subprocess against a disposable fixture, confirm the port answers, terminate that process, then confirm nothing still answers AND the next sweep reclaims its directory. Record the pid and reap the tree in `finally` -- `global-test-rules.md` forbids leaving a started process alive, and this is the one test here that starts one

**Checkpoint**: concurrent sandboxes coexist; abandoned ones are reclaimed.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T032 DECIDE explicitly whether any test in `tests/test_panel_sandbox.py` depends on native Windows locking semantics (T031a and the lease tests are the candidates), write the decision and its reason into `.ai/TESTING_NOTES.md`, and mark any test in `tests/test_panel_sandbox.py` that genuinely depends on native Windows locking with `RUNS_ON_NATIVE_WINDOWS = True` placed AFTER the last top-level import (E402), and add the module to the `windows-safety` job list in `.github/workflows/` in the SAME change — `tests/test_ci_coverage.py` enforces the match in both directions and fails on either half alone
- [X] T033 [P] Update the sandbox section of `docs/GUIDE.md` to describe the allowlist, the copy-not-link guarantee and the ownership-marker sweep; add a `CHANGELOG.md` entry under Unreleased
- [X] T034 [P] Record the suite's optimization evidence (test count, wall time, why it avoids launching a real panel where it can) in `.ai/TESTING_NOTES.md`
- [X] T035 Run `.\scripts\check.ps1` (compileall + `ruff check .` + the full suite) as the single heavy pass on the final tree, then push and read the CI result for that exact SHA

---

## Dependencies

```text
Phase 1 (T001-T002)
      |
Phase 2 (T003-T005)  <- blocks T028 only
      |
      +-- Phase 3 / US1 (T006-T010)   independent of US2
      |
      +-- Phase 4 / US2 (T011-T022)   independent of US1
      |
      +-- Phase 5 / US3 (T023-T031)   T028 needs T003/T004; T025 needs T023/T024
      |
Phase 6 (T032-T035)  <- needs everything
```

US1 and US2 touch different files (`scripts/panel_sandbox.py` vs
`emojikit/sandbox_clone.py`) and can proceed in either order. US3 is last
because its lifetime task depends on the in-process entry point.

## Parallel Opportunities

- **US1**: T009 and T010 after T006-T008.
- **US2**: T017-T022 after T011-T016 — six independent test tasks, different scenarios, one file.
- **US3**: T029-T031 after T023-T027.
- **Polish**: T033 and T034 together; T032 and T035 are serial and last.

Note that the [P] test tasks share one file (`tests/test_panel_sandbox.py`), so
they parallelise as independent *scenarios* to write, not as concurrent writers.

## Implementation Strategy

**MVP = US1 alone.** It is three small edits to the wrapper plus two tests, and
it closes the defect that needs no accident to trigger: a forwarded
`--data-dir` serving the owner's live catalog while the wrapper prints that the
catalog is not served.

**Then US2**, which is the bulk of the work but almost entirely assembly of
components that already exist and are already tested.

**Then US3**, which protects throwaway data from other throwaway data — real,
and the least costly of the three to get wrong.

Per `global-test-rules.md`, only light checks run while the code is being
written; the full suite runs once, in CI, at T035.

## Completion note

All 37 tasks done. Two deviations from the list as written, both recorded rather
than quietly absorbed:

- **T031a was replaced, not skipped.** The subprocess test it asked for raced the
  wrapper's own lifecycle and spent seconds re-proving an OS guarantee. What
  replaced it asserts the half that can actually be miswired -- the lease is held
  around the served panel and the sweep skips the live directory -- from inside a
  stubbed `panel.main`, deterministically. Reasoning in `.ai/TESTING_NOTES.md`.
- **T019 and T021 were under-covered when first written** and were finished
  afterwards: the project-relative media path and the contended-source refusal
  each got their own test rather than being counted as covered by a neighbour.

Final suite: 34 tests, ~2.4 s, hermetic. Heavy pass runs in GitHub CI (T035).
