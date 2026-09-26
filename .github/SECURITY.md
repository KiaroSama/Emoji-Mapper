# Security Policy

## Secrets

Numera Emoji Mapper talks to the Telegram Bot API and (optionally) CoinMarketCap. All
secrets are read from environment variables or a local `.env` file and are
never hard-coded in source:

- `TELEGRAM_BOT_TOKEN` — crypto-coin bot token
- `GENERAL_BOT_TOKEN` — general (non-coin) bot token
- `CMC_API_KEY` — optional CoinMarketCap Pro API key
- `PACK_OWNER_USER_ID` — numeric Telegram user id (pack owner)

Rules:

- Never commit `.env`. Use `.env.example` as a template.
- Never use a real credential as a test fixture. `tests/test_logsetup.py`
  enforces this: it fails if any secret-length value from `.env` appears in a
  git-tracked file.
- Bot tokens must not reach stdout, stderr or a log file. The token is embedded
  in every request URL, so exception text is redacted before printing
  (`Telegram._safe`), and the uncaught-exception hook renders its own redacted
  traceback rather than delegating to the default hook. If you add logging or a
  new `print` on an error path, route the text through `logsetup.redact`.
- A leaked bot token should be revoked immediately via @BotFather (`/revoke`),
  then update `.env`. Removing a secret from a file does **not** remove it from
  git history — rotate the credential as well.

## Reporting a vulnerability

If you find a security issue, please open a private report (or contact the
maintainer) rather than filing a public issue. Include reproduction steps and
the affected file/command. Do not include real tokens in any report.
