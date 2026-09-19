# Quickstart: validating Sandbox Isolation

How to prove this feature works. Details of what each requirement means live in
[spec.md](spec.md); the argument surface is in [contracts/cli.md](contracts/cli.md).

## Prerequisites

- the project venv (`.venv\Scripts\python.exe`)
- no real panel running on `panel.DEFAULT_PORT`
- **a disposable fixture catalog.** Never point any of this at `collection/`:
  reordering is what the panel does, so a synthetic drag IS a write, and that is
  the incident this feature exists to prevent (Constitution IV).

## 1. Automated check (the one that gates the work)

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_panel_sandbox.py"
```

`-t .` is not optional anywhere in this project: without it `tests/__init__.py`
never runs, and that file is the credential scrub and the non-loopback socket
block. `scripts\check.ps1` owns the correct form for the whole suite.

Expected: every test passes, including the five credential tests already present
before this feature.

## 2. Clone fidelity, by hand

```powershell
.venv\Scripts\python.exe scripts\panel_sandbox.py --source tests\fixtures\<disposable> --port 8766
```

Then, against the temp directory it prints:

- **no shared storage** — on Windows compare `fsutil file queryFileID` for a
  clone file and its source; on POSIX compare `stat -c %i`. They must differ.
- **no source paths** — every `items.file_path` in the clone's `catalog.db`
  must start with the clone directory.
- **the plan came along** — `pack_plan.json` exists in the clone when the source
  had one.
- **row counts match** — the clone's `COUNT(*)` equals the source's, including
  rows that were still only in the source's WAL.

## 3. Refusals

Each of these must exit non-zero, print a reason, and leave no new directory:

```powershell
.venv\Scripts\python.exe scripts\panel_sandbox.py --data-dir collection
.venv\Scripts\python.exe scripts\panel_sandbox.py --sou tests\fixtures\<disposable>
.venv\Scripts\python.exe scripts\panel_sandbox.py --nonsense
```

The first is the defect this feature was opened for: before the change it
started a panel on the owner's real catalog while printing that the real catalog
was not served.

## 4. Two sandboxes at once

Start one, leave it running, start a second on another port. The first must keep
serving and its clone directory must still exist — the previous sweep deleted
every directory matching the name prefix, live or not.

Kill the first without letting it clean up. The next start must reclaim its
directory, and must leave the lock file behind.

## 5. What this does NOT prove

Cooperative isolation between the owner's own tools. A process determined to
reach `collection/` still can. The claim being tested is that the sandbox does
not reach it by accident, which is how the damage actually happened.
