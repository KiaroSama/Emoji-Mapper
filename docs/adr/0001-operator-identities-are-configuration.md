# Operator identities are configuration, never source

The repository is public, and it had been shipping one operator's identity as
if it were the project's: two bot identities, the pack bases and titles built
from them, a brand logo, and — as test fixture data — a numeric Telegram user id
and two chat ids. On 2026-09-26 all of them moved to per-operator configuration
(`.env` for the Python tools, Worker variables for the Cloudflare Worker), and
the repository's history was rewritten to replace every earlier occurrence with
neutral placeholders.

A value that identifies an operator has **no default**. A missing pack base,
pack title or brand logo stops the tool with a message naming the key, because
a default would publish under a name, or with a logo, that nobody chose — and a
pack name is permanent once Telegram has it.

## Considered Options

- **Keep the identities as shipped defaults, overridable by configuration.**
  Rejected: every clone would still publish as the original operator unless it
  noticed, and the history would still name them.
- **Neutral defaults (`cryptoemoji`, a generic logo).** Rejected for the same
  permanence reason; an unconfigured run should stop, not guess.

## Consequences

- Tests use synthetic identities; a test that needs "the real bot" is a test
  that cannot run on anyone else's installation.
- The rewrite changed every commit hash. GitHub still serves the pre-rewrite
  commits through existing pull-request references; only GitHub Support can
  remove those, and that was deliberately not requested.
