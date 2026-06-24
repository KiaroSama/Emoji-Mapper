# Security Policy

## Secrets

Emoji Mapper talks to the Telegram Bot API and (optionally) CoinMarketCap. All
secrets are read from environment variables or a local `.env` file and are
never hard-coded in source:

- `TELEGRAM_BOT_TOKEN` — crypto-coin bot token
- `GENERAL_BOT_TOKEN` — general (non-coin) bot token
- `CMC_API_KEY` — optional CoinMarketCap Pro API key
- `PACK_OWNER_USER_ID` — numeric Telegram user id (pack owner)

Rules:

- Never commit `.env`. Use `.env.example` as a template.
- Bot tokens are never printed, logged, or embedded in URLs/exceptions shown to
  users. If you add logging, redact token values.
- A leaked bot token should be revoked immediately via @BotFather (`/revoke`),
  then update `.env`.

## Reporting a vulnerability

If you find a security issue, please open a private report (or contact the
maintainer) rather than filing a public issue. Include reproduction steps and
the affected file/command. Do not include real tokens in any report.
