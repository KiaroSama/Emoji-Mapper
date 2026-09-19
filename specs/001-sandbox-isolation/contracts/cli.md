# Contract: `scripts/panel_sandbox.py` command line

The wrapper's only external interface. The contract is an ALLOWLIST: anything
not named here is refused, never forwarded.

## Accepted

| Option | Type | Meaning |
|---|---|---|
| `--source <dir>` | path, default `collection` | the catalog to CLONE. Never served. |
| `--port <n>` | int, default `panel.DEFAULT_PORT + 1` | the sandbox's port |
| `--all` | flag | forwarded: show already-published emoji too |
| `--with-pack <n>` | int, repeatable | forwarded: include the named published pack |
| `--bot-username <name>` | string | forwarded: branding without contacting Telegram |
| `-h` / `--help` | flag | usage |

## Refused, with a non-zero exit and nothing cloned or served

| Input | Reason |
|---|---|
| `--data-dir <anything>` | the defect: it would select what gets served |
| any option not in the table above | no generic forwarding exists any more |
| an abbreviation such as `--sou`, `--wi` | `allow_abbrev=False`; an abbreviation that is unambiguous today becomes a different option the day another is added |
| `--port` equal to `panel.DEFAULT_PORT` | that is the real panel's port |
| `--port` outside 1-65535 | not a port |
| a `--source` that does not exist, or holds no `catalog.db` | nothing to clone |

## Refused at clone time

| Condition | Exit behaviour |
|---|---|
| another writer holds the source | `LockBusy` reported, nothing created |
| the source has an interrupted migration journal | `LockBusy` reported, naming how to resume |
| the destination exists, or overlaps the source | refused; an occupied directory is never erased |
| a referenced media file is missing or unreadable | the incomplete destination this attempt created is removed; the source is untouched |
| the snapshot exceeds its 30 s budget | `BackupTimeout`; incomplete destination removed |

## Guarantees while running

- stdout states the item count, the source path, and that the source is not served;
- the panel runs in this process, so the wrapper's exit stops the server and
  releases its lease together;
- `EMOJI_MAPPER_NO_DOTENV=1` is set and credential-shaped variables are removed
  for the duration, then the environment is restored;
- session reuse is off, so no pre-existing listener is adopted as the sandbox.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the panel ran and exited normally |
| 2 | argument refused (argparse) |
| non-zero, other | clone refused or failed; message names the reason |
