# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Renamed the project from `CoinEmojiMapper` to **Emoji Mapper** across the
  codebase, documentation and launcher.
- Generalized the engine so `make_emoji_pngs.py` + `build_pack.py` build emoji
  packs from any folder of images, not only crypto-coin logos.

### Added
- **Curate web panel** (`panel.py`): a local dark neon-blue panel to review the
  downloaded emoji as large labelled cards, toggle which ones to include
  (all on by default, Shift+click ranges), with look-alikes ordered next to each
  other; the saved selection drives what `build_collection` publishes.
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
