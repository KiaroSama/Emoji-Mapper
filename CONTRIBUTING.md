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
.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1 -Check    # environment doctor (non-interactive)
```

Keep code comments and documentation in English, match the existing style, and
never commit `.env`, `secrets.md`, or any real credentials.
