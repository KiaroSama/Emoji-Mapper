# Persistence audit — 2026-09-19

**Respect all owner-defined repository, workspace, global instructions and Git hooks. Never disable checks, bypass hooks, force-push, merge or close this PR on the auditor's behalf. The owner's reviewing agent makes the integration decision.**

Baseline: `bfd0cdc8336dda292febc683b1e52cac9e15d12b`.

The earlier native-frame collision, maintenance-lock and rollback-bundle repairs are retained. This change follows the newer curation feature through every persistence boundary instead of repeating the previous reports.

## Confirmed failures and implemented repairs

1. **Curation state omitted from migration and rollback.** `pack_plan.json` was outside `publish_*.json`, so a successful migration left its IDs stale and its bytes absent from rollback. `state_artifacts` defines one inventory for discovery, mapping, verification and rollback. It maps only schema-defined IDs, not labels; malformed or stale required references refuse before mutation. Complete target/scope/exclusion fields are included. A real-video HTTP-save → migration → reload → restore regression verifies the composition, not just the helpers.
2. **Incomplete selection revisions.** Pack assignments were read from the live UI on every retry, excluded from dirty tracking and excluded from the permanent-refusal signature. An old 400 could block a newer pack-only Save, and a failed Save could submit later unsaved changes. Each explicit Save now captures its complete body; retries and refusals use that immutable body, and acknowledgements describe only the submitted revision. Reset/undo/redo also restore absent assignments, rather than retaining a later pack stamp.
3. **False or wedged acknowledgements.** A 200 with `{ok:false}`, a malformed JSON body or a body-read deadline could clear work. A null 400 body threw while the flight stayed claimed. The shared client now requires a complete positive response with valid counts, propagates unsuccessful reads of successful responses, and normalizes malformed error bodies without losing their status. Both queues retain work and remain usable.
4. **Saved intent lost on reload or partial-scope Save.** The page reconstructed only live membership; the next Save erased the move. Save also overwrote decisions outside its visible scope. The saved plan now retains full targets, scope and exclusions; rendered cards receive intended targets without overwriting the server's live membership. Scoped writes merge outside decisions, preserve legacy delta-only plans, and reject invalid/duplicate/out-of-scope targets before mutation. Logo capacity is reported per pack.
5. **Writer exclusion stopped before the plan write.** The Catalog lease closed before `write_plan`, allowing migration to interleave. Both permanent writes now share that lease; stale server-view identities are checked against the current database before Save/order mutation. Plan I/O failure is a non-acknowledged 503 and an exact retry is safe. This is not a claim that SQLite and JSON replacement form one atomic transaction.
6. **Unbounded SQLite backup retries.** `Connection.backup` can continue BUSY/LOCKED retries beyond `sqlite3.connect(timeout=...)`. The shared bounded snapshot helper is used for capture, projection and restoration, restores connection settings, and removes only a failed capture's own partial output. Tests hold real source/destination locks and confirm recovery after release. The deadline bounds SQLite steps/retries, not uninterruptible operating-system I/O.
7. **Sample cache aliases relative paths and ignores the toolchain.** Two different files at `clip.webm` in different working directories with identical size/mtime returned the first file's cached pixels. Sample/decoder cache keys now use canonical paths; sample caches also bind the FFmpeg identity, matching the native timeline cache. Real clips and toolchain-change regressions cover both cases.

## CI portability correction

The VFR fixture omitted terminal packet duration on FFmpeg 7: container duration equaled the final PTS. Production correctly refused that incomplete timing, but the test required `False`. The fixture now supplies an explicit 33 ms packet duration in its 1/1000 time base, retaining the 10 ms differing native frame. The test verifies native timestamps and terminal duration. No production comparison tolerance was weakened.

Existing Linux/Python 3.11/3.12, browser and Worker suites stay mandatory. The browser regressions extend the already registered queue suite. A separate hosted Windows gate exercises native locks, migration/restore, new persistence regressions, real video identity and CLI contracts. Existing Dependabot pip/npm/Actions coverage is retained without duplicate configuration.

## Review and closure

Review PR #33's actual code and exact head checks, not this prose as a substitute for execution. Run `./scripts/check.ps1`, the mandatory browser suites, Worker `npm ci`, typecheck and Vitest, and the native Windows gate. Verify the new negative cases against the baseline and the corrected behavior after applying the patch. Keep credential/network guards and `-t .` discovery intact.

Merge only after owner hooks, required checks and approval succeed. When changes are necessary, finish them in the same PR, rerun affected and full gates, then integrate through the permitted workflow. If integration must be done manually, prove the same fixes/tests reached the default branch before closing the PR. Do not close it merely to make a checklist green. The auditing assistant leaves the PR open and does not merge it.

Optional, not blockers invented by this audit: assess optimistic multi-tab revisions, import/validation of exported drafts, and a durable combined save transaction/outbox if crash-level atomicity across SQL and JSON is required. Such additions need their own complete migration/rollback inventory, not another untracked key-bearing artifact.
