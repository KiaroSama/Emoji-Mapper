# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Emoji Mapper bot reply** reworked: sending one or more premium emoji (any
  spacing/newlines) now returns two formats — (1) a **collapsed (expandable)**
  quote of the **actual premium emoji** (rendered via `<tg-emoji>`) + `<code>`
  ID per line for individual tap-to-copy, and (2) a `<pre>` block of all IDs
  nested in a collapsed quote, with Telegram's Copy button for reliable one-click
  copy-all (desktop + mobile). Both sections are collapsed (expandable) quotes;
  huge lists split across messages; a rejected custom emoji falls back to plain
  fallback chars.
- **`build_collection.py`** now places the **God Verify logo as the first emoji
  of every set** built with `@GodVerifyEmojiMapperbot`. Since Bot API 7.2 a
  single set may contain mixed formats, so the logo is always a static 100x100
  PNG and leads static, video AND animated sets alike (verified live). The coin
  bot is exempt. New `--brand-logo` / `--no-brand-logo` flags.
- Renamed the project from `CoinEmojiMapper` to **Emoji Mapper** across the
  codebase, documentation and launcher.
- Generalized the engine so `make_emoji_pngs.py` + `build_pack.py` build emoji
  packs from any folder of images, not only crypto-coin logos.
- **Curate panel** thumbnails now use a dark-slate contrast checkerboard
  (`#828c9a`/`#464e5a`) plus a Backdrop switch (Checker/Light/Dark/Gray,
  persisted to `localStorage`) so black, hollow-center and faint emoji stay
  clearly visible while matching the dark theme.

### Added
- **`fetch_emoji_ids.py`** — download only *specific* premium custom-emoji by
  their IDs (e.g. `premium-id:<n>` entries from bot inventory files) instead of
  whole packs. Reads only real `premium-id:` entry lines (start-anchored, so
  example/prose mentions are skipped and trailing labels/emoji are still
  captured), de-duplicates IDs within a file and across files (each real emoji
  fetched once) and again by content, resolving via `getCustomEmojiStickers`
  into the catalog. New `Telegram.get_custom_emoji_stickers()` helper (batched,
  ≤200 IDs/call). Extraction/dedup covered by `tests/test_fetch_emoji_ids.py`.
- **Advanced logging** (`emojikit/logsetup.py`): per-run id on every line,
  automatic secret-value + token redaction across all records and tracebacks
  (content hashes/ids preserved), rich file format, optional JSONL sidecar,
  uncaught-exception capture, `log_duration`/`logcall` helpers, third-party
  noise reduction, and an end-of-run warnings/errors summary.
- **Curate web panel** (`panel.py`): a local dark neon-blue panel to review the
  downloaded emoji as large labelled cards — static/video previewed and
  **animated `.tgs` rendered & looped via a vendored Lottie player** (lazy,
  IntersectionObserver-driven so only on-screen animations play) — toggle which
  ones to include (all on by default, Shift+click ranges), with look-alikes
  ordered next to each other; the saved selection drives what `build_collection`
  publishes.
- **Emoji Mapper bot** (`emoji_bot.py`): interactive long-polling bot that
  extracts premium custom-emoji IDs from sent/forwarded messages and from new
  channel/group posts, with tap-to-copy (`copy_text`) inline buttons and a
  `/start` help menu.
- **Multi-format collector workflow**: download emoji from existing Telegram
  packs (`fetch_pack.py`), build emoji from scratch (`add_media.py`) and
  republish into new per-format packs (`build_collection.py`).
- Support for **animated** (`.tgs`) and **video** (`.webm`/VP9) custom emoji in
  addition to static, including GIF/MP4 → WEBM conversion via ffmpeg.
- `emojikit/` core toolkit: UTC file logging, media detection/hashing/conversion
  and a content-addressed SQLite **catalog** that deduplicates at ingest time
  (by `file_unique_id`, content hash and perceptual hash) and makes publishing
  idempotent and resumable — eliminating the old delete-and-rebuild churn.
- `tests/` unit suite (stdlib `unittest`) with a committed Lottie fixture.
- Proprietary `LICENSE` (All Rights Reserved).
- GitHub repository support files: issue/PR templates, Dependabot config and
  this changelog.

## [1.0.0] - 2026-06-24

### Added
- Core engine: image → 100×100 PNG conversion (`make_emoji_pngs.py`) and
  resumable Telegram custom-emoji pack uploader (`build_pack.py`).
- `coins/` component: logo fetchers (CoinGecko / CoinPaprika / CoinMarketCap),
  keyword builder, duplicate-proof rebuild and pack integrity audit.
- PowerShell launcher (`run.ps1`) with general and crypto-coin workflows.
- CI workflow with byte-compile, import smoke test and offline dry-run.
