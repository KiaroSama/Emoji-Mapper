# Contributing

Emoji Mapper is **proprietary** software released under an All Rights Reserved
license (see [LICENSE](LICENSE)). It is published for reference and authorized
use only.

## External contributions

This is not an open-source project. Pull requests from outside contributors are
generally **not accepted** unless arranged in advance through a written
agreement with the copyright holder. By submitting any contribution you confirm
that you have the right to do so and you assign all rights in that contribution
to the copyright holder.

## Reporting issues

Bug reports and feature suggestions are welcome via the GitHub issue tracker.
When reporting a bug, please include:

- the command you ran (with secrets redacted),
- the workflow used (general or crypto-coin),
- your Python version and operating system,
- the full error output (never include real bot tokens or API keys).

## Security

Do not file security issues publicly. Follow the process in
[SECURITY.md](SECURITY.md).

## Local development

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt         # core deps
.venv\Scripts\python.exe -m pip install -r requirements-coins.txt   # only for coins/remap_ids.py
.venv\Scripts\python.exe -m pip install ruff                        # linter (not a runtime dep)
.\run.ps1 -Check      # environment doctor (non-interactive)
.\scripts\check.ps1   # byte-compile + ruff + full unit suite — what CI runs
```

Lint is `ruff check .` with no arguments; `ruff.toml` at the repo root owns the
rule set and the exclusions. Do not pass `--select`/`--exclude` on the command
line and do not silence a finding with `--exit-zero` — a lint stage that cannot
fail is worse than none.

Run `scripts\check.ps1` before every commit rather than a hand-written
`unittest` command: the suite must be started as
`python -m unittest discover -s tests -t . -p "test_*.py"`, and dropping `-t .`
disables the test-suite credential/network guard (see
[`tests/README.md`](tests/README.md)).

Keep code comments and documentation in English, match the existing style, and
never commit `.env`, `secrets.md`, or any real credentials.
