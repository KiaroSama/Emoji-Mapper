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
