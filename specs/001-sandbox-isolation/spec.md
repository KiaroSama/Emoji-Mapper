# Feature Specification: Sandbox Isolation

**Feature Branch**: `001-sandbox-isolation`

**Created**: 2026-09-19

**Status**: Draft

**Input**: Audit 5, item R2. Seven defects verified against the current
`scripts/panel_sandbox.py` before this spec was written; none is hypothetical.

## User Scenarios & Testing *(mandatory)*

The owner curates a real emoji collection through the panel. An agent verifying
that panel has to drive it, and driving it means dragging, which means writing.
The sandbox exists so those writes land on a copy. Today it makes a copy and
still leaves several paths open back to the original — so the copy is a
reassurance rather than a boundary.

### User Story 1 - The sandbox cannot be talked into serving the real catalog (Priority: P1)

The owner or an agent starts the sandbox, possibly passing presentation options
through to the panel (`--all`, `--with-pack N`). Whatever is passed, the thing
that gets served is the throwaway copy.

**Why this priority**: it is the only defect that hands over the real catalog in
one step, with no filesystem accident required. The wrapper forwards unknown
options to the panel and appends them AFTER its own `--data-dir`, so
`panel_sandbox.py --data-dir collection` makes the panel read the owner's live
directory while the wrapper prints that the real catalog "is NOT served".

**Independent Test**: pass a data-directory override, a misspelled option and an
abbreviated option; each is refused, and nothing is cloned or served.

**Acceptance Scenarios**:

1. **Given** the sandbox is started with a data-directory override among its
   arguments, **When** it parses them, **Then** it exits with an error naming the
   rejected option and serves nothing.
2. **Given** an option outside the presentation allowlist, **When** it parses,
   **Then** it is refused rather than forwarded.
3. **Given** an unambiguous abbreviation of an allowed option, **When** it
   parses, **Then** it is refused, because an abbreviation that is unambiguous
   today becomes a different option the day another is added.

### User Story 2 - Everything the sandbox serves is a private copy (Priority: P1)

The clone holds its own bytes. Nothing the sandbox serves, and nothing it could
write, is shared with the source.

**Why this priority**: the sandbox is justified to the owner as "it cannot touch
your data". Three separate paths break that promise: clone media is hard-linked,
so the two directories are the same bytes; a media path outside the source tree
is left pointing at the owner's archive; and a plain file copy of the database
omits an uncheckpointed write-ahead log, so the clone can be missing committed
rows or, at worst, arrive without its tables.

**Independent Test**: clone a source with an open WAL, an internal file, an
external archived file and a project-relative file; then verify the clone's row
count, that no clone file shares an inode with its source, and that no remaining
`file_path` points outside the clone.

**Acceptance Scenarios**:

1. **Given** a source catalog with committed rows still in its write-ahead log,
   **When** it is cloned, **Then** the clone contains those rows.
2. **Given** a media file referenced from inside the source directory, **When**
   it is cloned, **Then** the clone's copy is independent of the source's bytes.
3. **Given** a media file referenced from outside the source directory (the
   owner's archive), **When** it is cloned, **Then** it is copied in and the row
   points at the copy, never at the archive.
4. **Given** any row in the cloned catalog, **When** its path is read, **Then**
   it resolves inside the clone.
5. **Given** a source carrying curation intent, **When** it is cloned, **Then**
   that intent is present in the clone, so the sandbox shows the layout the owner
   actually saved.

### User Story 3 - One sandbox never destroys another, and a dead one is reclaimed (Priority: P2)

Two sandboxes can run at once. Starting the second leaves the first alone. A
sandbox killed without cleanup has its directory reclaimed by the next run.

**Why this priority**: less severe than reaching the real catalog, because the
damage is confined to throwaway data — but the current sweep deletes every
directory matching its name prefix, so on a filesystem that permits deleting an
open directory the second sandbox removes the catalog the first one is serving.

**Independent Test**: mark one directory as owned and running, one as owned and
abandoned, one unmarked, one marked by a foreign owner, and one a symbolic link;
sweep, and confirm only the abandoned one is gone.

**Acceptance Scenarios**:

1. **Given** a sandbox directory currently being served, **When** another sandbox
   starts, **Then** that directory survives.
2. **Given** a sandbox directory whose server has died, **When** another sandbox
   starts, **Then** it is removed.
3. **Given** a temp directory with no ownership marker, a marker belonging to
   someone else, or a symbolic link, **When** the sweep runs, **Then** each is
   left untouched.
4. **Given** the sandbox server stops for any reason, **When** its process ends,
   **Then** its claim is released, so nothing keeps serving a deleted copy.

### Edge Cases

- The source is being written by another process, or a migration is pending:
  the clone is refused rather than taken from a moving target.
- The destination already exists, or overlaps the source: refused; an occupied
  directory is never erased to make room.
- Copying fails halfway: only what this attempt created is removed, and the
  source is untouched.
- A row names a media file that is missing or unreadable: the clone is abandoned
  rather than completed with a row still pointing at production.
- The lock file of a still-running sandbox must survive the sweep, because on
  POSIX an unlinked inode leaves the lock holder invisible to the next run.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The wrapper MUST accept only an explicit allowlist of presentation
  options, MUST disable option abbreviation, and MUST refuse any unknown option
  or data-directory override instead of forwarding it.
- **FR-002**: The wrapper MUST hold the source's writer claim from before it
  begins reading until after the copy is complete, and MUST refuse safely when
  another writer holds it or a migration is pending.
- **FR-003**: The catalog MUST be copied by a bounded online snapshot that
  includes committed write-ahead-log content, not by a plain file copy.
- **FR-004**: Every media file the catalog references MUST be copied into the
  destination as independent bytes — internal, external and project-relative
  alike — with no links to the source.
- **FR-005**: Every path stored in the cloned catalog MUST resolve inside the
  destination after cloning. No row may retain a source path.
- **FR-006**: The shared state inventory, including the curation plan, MUST be
  copied into the destination.
- **FR-007**: A failed clone MUST remove only the incomplete destination this
  attempt created, MUST NOT erase an occupied destination, and MUST leave the
  source's data unchanged.
- **FR-008**: A destination MUST be new and disjoint from the source.
- **FR-009**: Reclaiming an abandoned sandbox directory MUST require BOTH a valid
  ownership marker bound to that directory AND acquisition of the same claim its
  server held. Active, unmarked, foreign-marked and symbolic-link directories are
  left alone.
- **FR-010**: The sweep MUST retain the small lock artefact of a directory it did
  not reclaim, so a live claim is never made invisible to the next run.
- **FR-011**: The panel MUST run inside the wrapper's own process with session
  reuse disabled, so that the wrapper's death stops the server and releases its
  claim together, and so no unidentified listener is adopted as the sandbox.
- **FR-012**: Dotenv loading and credential-shaped inherited variables MUST be
  suppressed for the panel invocation and the environment restored afterwards.
  *(Already implemented and covered by `tests/test_panel_sandbox.py`.)*

### Key Entities

- **Source**: the owner's real catalog directory — read, claimed, never written.
- **Destination (clone)**: a fresh throwaway directory holding its own database,
  state files and media bytes; deleted when done.
- **Ownership marker**: a small record inside a sandbox directory naming who
  created it, paired with a claim the operating system can test for liveness.
  One without the other is not proof of abandonment.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: No argument the wrapper accepts causes the owner's real catalog
  directory to be served. Attempting it produces a refusal, not a server.
- **SC-002**: After a clone, zero rows in the cloned catalog reference a path
  outside the clone, and zero cloned files share storage with a source file.
- **SC-003**: A clone taken from a source with uncommitted-to-disk but committed
  rows reports the same item count as the source.
- **SC-004**: Running two sandboxes concurrently leaves both serving; neither
  loses its data to the other's start-up sweep.
- **SC-005**: Killing a sandbox leaves no process still serving, and the next
  start reclaims its directory.
- **SC-006**: Every clone failure mode leaves the source byte-identical apart
  from lock-diagnostic records that normal claim acquisition writes.

## Assumptions

- This is cooperative test-data isolation between the owner's own tools, not an
  operating-system boundary against hostile code running as the same user. A
  process determined to reach the source can; the point is that the sandbox does
  not do so by accident, which is how the damage actually happened.
- Media volume stays in the order of the current collection (about 1000 files),
  so copying rather than linking is an acceptable start-up cost. The audit's own
  figure is roughly 20 MB per 200 emoji.
- The panel remains importable as a function taking an explicit argument list;
  ordinary command-line invocations keep their existing reuse behaviour.

## Constitution Alignment

- **The Suite Can Never Reach Telegram** — FR-012 is this principle applied to
  the sandbox; FR-004 and FR-005 extend the same reasoning to the owner's files.
- **Unknown Is Not False** — FR-009: a directory whose owner cannot be determined
  is not thereby abandoned, and FR-001: an option that cannot be recognised is
  not thereby harmless.
- **The Owner's Packs Are Live Data** — the whole feature.
