# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — second audit pass (34 findings)
- **The bot rejected everyone, including its owner.** `main()` built the numeric
  allowlist as `allowed`, then reassigned that same name to the Telegram
  update-type filter and passed it to the handler, so the check compared a user
  id against `["message", ...]` and was always true. The regression test now
  runs through the real polling wiring, which is where the defect lived.
- **Unresolved uploads no longer let the run continue.** An ambiguous
  add/create used to be logged while the loop moved on, and the next item's
  in-flight record overwrote the unresolved one — after which a later run could
  re-send an upload that had already landed. The run now stops with a retryable
  exit until the ambiguity is settled, and the in-flight record is structured
  (operation, target set, index, expected count) so even an ambiguous *create*
  whose set was never recorded can be reconciled by name.
- **"Unknown" is no longer read as "empty".** Live set reads are tri-state
  (EXISTS / MISSING / UNKNOWN). Previously any network error became a live count
  of zero, and a probe failure could clear the only record of an unresolved
  mutation.
- **Corrupt state and plans fail closed.** A file that exists but cannot be
  parsed is an error; only an absent file may fall back to a default. All state,
  plan and canonical-map writes are atomic.
- **Concurrent publishers are refused** via an exclusive per-state lock.
- **Live drift is detected by identity, not position.** The full set manifest is
  verified by `file_unique_id`, so a manual delete/shrink/reorder/replace inside
  the recorded prefix is caught, and a `custom_emoji_id` is never assigned from
  a positional guess.
- **Partial failure never reports success.** `build_pack` and `fetch_pack` now
  return 3 (partial) or 4 (failed); a state file whose base disagrees with the
  run is an integrity error instead of a silent restart from zero.
- **Every ffmpeg/ffprobe child is bounded**; a hang becomes a `MediaError`
  instead of stalling ingest forever.
- `coins/verify_logos.py` died on an import that no longer existed; all 22
  entry points, including every `coins/*` module, now import cleanly.
- Provider tools (CoinPaprika/CMC) use one verified mutation path with
  `expected_before` instead of blind retries, reject blank logos, and no longer
  map emoji ids from the tail of a set.
- Smaller contracts: `--formats`/`--per-set`/`--limit` validated, perceptual
  hash threshold bounded inside `Catalog`, a broken SVG falls back to a healthy
  same-stem raster, the panel bounds TGS decompression, `EMOJI_LOG_RETENTION_DAYS`
  parses safely, cached logo files are validated, and an ambiguous normalized
  coin name is reported rather than auto-resolved to the first candidate.

### Security
- **Real credentials were committed as test fixtures.** The live
  `GENERAL_BOT_TOKEN` and `CMC_API_KEY` were hard-coded in
  `tests/test_logsetup.py`. They are replaced with synthetic values and
  `TestNoCommittedSecrets` now fails if any secret-length `.env` value appears
  in a git-tracked file. **Both credentials must be rotated** — they remain in
  git history.
- **Bot token could reach stdout/stderr.** The token is embedded in every
  request URL, and a `requests` exception carries that URL; exception text is
  now redacted before printing, and the uncaught-exception hook renders its own
  redacted traceback instead of delegating to the default hook, which printed
  it raw.
- **Curate panel: stored script injection.** Catalog labels (which come from
  downloaded packs) were embedded in an inline `<script>`; a label containing
  `</script>` escaped the data block. Item data is now parsed from an inert
  JSON block and written with `textContent`.
- **Curate panel: unauthenticated mutation.** Any page in the browser could
  POST to the localhost panel and change inclusion or publish order. Mutations
  now require a per-run token, a loopback `Host`/`Origin`, `application/json`,
  and a bounded body; `/api/order` must be an exact permutation.

### Fixed
- **Duplicate uploads on resume.** Resume position was derived from the live
  sticker count, but a skipped plan entry (missing, blank or failed image)
  consumes a plan position without producing a sticker — so each skip shifted
  the cursor back by one and the next run re-uploaded already-published
  entries. `build_pack.py` and `coins/rebuild_dedup.py` now resume from a
  recorded cursor plus a write-ahead in-flight record, and refuse to continue
  when live state and the record disagree instead of guessing.
- **Corrupted canonical coin map.** The same drift was written into
  `coins/ticker_to_id.json`: 4202 of 5962 entries had been rewritten and one
  emoji id was claimed by 129 unrelated tickers. Restored from the
  pre-corruption backup; the positional mapping fallback that produced it is
  removed, and a duplicate-upload / oversized-shared-group guard now runs
  before any map write.
- **False coin aliases.** `enhance_map.py` treated a single trailing `c` as a
  chain suffix, mapping `ghc`→`gh` and `zbc`→`zb`. The heuristic is removed;
  the two genuine cases (`avaxc`, `bttc`) are named aliases.
- **Animated emoji canvas.** Telegram requires a 512×512 Lottie canvas for
  `.tgs`; the converter rewrote every animation to 100×100 by scaling the
  top-level layer transform, which clips artwork, and the validator never
  checked. The canvas is now preserved, other sizes are rejected, and
  validation covers gzip packaging, frame rate, and duration.
- **State files could be truncated.** Progress is now written atomically, and
  unreadable state aborts instead of silently restarting from zero (which
  re-uploaded everything).
- **Six-minute stall on a missing set.** `STICKERSET_INVALID` was retried for
  every method; it is now scoped to set creation with an overall deadline, and
  no sleep follows the final attempt.
- **Per-set default exceeded Telegram's cap** (400 vs the documented 200), and
  `--per-set 0` reached a division by zero. Bounds are validated.
- `TELEGRAM_API_BASE` set only in `.env` was ignored, because the base was read
  at import time — before `load_env()` runs.
- Rebuild deleted the old packs before checking the plan was usable.

### Changed
- **SVG rasterizing moved from svglib+reportlab to `resvg-py`.** svglib 2.x
  requires `reportlab>=4.4.3`, which the pinned `reportlab<4` blocked; that pin
  also parked the project on reportlab 3.6.13 (April 2023), whose `>=3.6` floor
  can resolve to a CVE-2023-33733-vulnerable build, and reportlab 5.0 removed
  the bundled renderPM backend outright. resvg ships a prebuilt self-contained
  wheel, needs no system cairo, and renders straight to RGBA — deleting the
  two-pass white/black render and the numpy alpha reconstruction, and fixing
  the long-standing blank-gradient bug. Nothing pins Python 3.11 any more; CI
  now runs 3.11 and 3.12.
- **Curate panel is substantially faster** on a 199-item catalog: first paint
  589 ms → 45 ms, and toggling a selection 13.6 ms → 1.2 ms. Selection and
  drag no longer rebuild the whole grid, videos no longer autoplay at rest,
  off-screen cards skip layout and paint, and the blocking webfont import is
  gone.
- Documentation: removed four logging helpers the guide documented but that
  never existed (its example import failed), and corrected the SVG/dependency
  claims.

### Added
- Project logos in `assets/`, shown in the README and the curate panel.
- **The emoji bot is now private.** `BOT_ALLOWED_USER_IDS` lists the numeric
  Telegram ids allowed to use it (defaulting to `PACK_OWNER_USER_ID`). An unset
  allowlist means *nobody* — the bot refuses to start rather than run open.
  Authorization is checked on the message sender, so a stranger cannot extract
  emoji ids; unauthorized group messages are ignored silently.
- **`PACK_LINKS_CHAT_ID`** announces finished-pack links in a channel (numeric
  id or `@username`) instead of the owner's private chat. The bot must be an
  administrator of that channel; unset keeps the previous PM behaviour.
- **`EMOJI_LOG_RETENTION_DAYS`** (default 30) prunes old run logs, which
  previously accumulated one file per run forever. The end-of-run summary now
  also reports the outcome and the real exit code.
- Publication records are queryable per pack family: `is_published(base, key)`,
  `custom_emoji_id_for(base, key)`, `publication_bases()` and
  `forget_publication(base)` to make a deleted pack family publishable again.
- **Curate panel — drag to reorder.** Emoji can now be dragged to set the
  **publish order**. The order is persisted to a new `items.position` column
  (`Catalog.set_order`, POST `/api/order`) and drives both the panel and
  `build_collection` (each per-format set publishes in this relative order). On
  first open the order is seeded to the look-alike similarity grouping.
- **Curate panel**: a distinct gold "Brand logo" preview card now appears first
  when the configured bot is `@YourEmojiBot` and the logo file
  exists, showing where the mandatory logo will be inserted on publish. It's
  preview-only (not clickable, not counted, never reordered, never sent to
  `/api/save`) since the logo is only actually added by `build_collection.py`
  at publish time.

### Changed
- **Curate panel performance**: animated `.tgs` are now shown as a **static
  first frame** and only play on **hover** (were all autoplaying+looping at
  once, which hung the page on large collections). Off-screen Lottie players are
  destroyed. Launcher menu label clarified to "Open web panel to pick & reorder
  emoji (browser)" and its start message shortened.
- **Curate panel**: benign browser disconnects while scrolling no longer spam
  the log with `ConnectionAbortedError` tracebacks (handled in the request
  handler and server `handle_error`).
- **Emoji Mapper bot**: each "Copy"/"Copy a-b" button's `copy_text` now ends
  with a trailing newline, so pasting the copied IDs leaves a blank line after
  the last one.
- **Emoji Mapper bot**: each "Copy"/"Copy a-b" button's `copy_text` now ends
  with a trailing newline, so pasting the copied IDs leaves a blank line after
  the last one.

### Fixed
- **Duplicate-proof uploads, verified end-to-end.** `addStickerToSet` /
  `createNewStickerSet` are not idempotent, and a network timeout after
  Telegram had already applied the call was blindly re-sent — the same emoji
  could land in a pack twice (both in the collector and the coin flows).
  `Telegram._call` now verifies the live set after a network failure
  (applied → success, not applied → safe retry, unknown → new
  `AmbiguousUploadError`), and `build_collection.publish_format` reconciles
  every applied-but-unrecorded live sticker back to its catalog item (by
  `file_unique_id`, else by downloaded content) before computing pending
  items — a crash between the upload and `mark_uploaded` can no longer cause
  a re-upload on resume. A create that landed ambiguously (or a leftover
  "occupied" set name) is now adopted instead of wedging every later run.
  Covered by `tests/test_publish_dedup.py`.
- **Upload retries no longer send an empty file.** Retried upload calls
  (flood wait or network retry) used an already-exhausted open file handle,
  so the retry posted a zero-byte body and the sticker failed; all upload
  methods now send the file content as bytes.
- **Own packs are never re-downloaded.** Publishing records each uploaded
  copy's `file_unique_id` in the catalog's `seen_files`, so a later
  `fetch_pack.py` / `fetch_emoji_ids.py` of our own published packs is caught
  by the fast pre-dedup and downloads nothing (previously every copy was
  re-downloaded and, for static, could even re-enter the catalog after
  Telegram's re-encode).
- **Emoji Mapper bot**: id batching now follows the message-length limit again
  (not the smaller `copy_text` button limit), so a 50-id reply is 2 messages as
  before, not 5. Telegram's `copy_text` button is still hard-capped at 256 chars
  (~12 ids), so a message with more ids than that shows a few chunked
  "Copy a-b" buttons together covering every id in that message — Telegram
  itself has no single-tap way to copy an arbitrarily large id list, and a
  single message is capped at 4096 chars (a 50-id message would need ~4500+),
  so very large results still need more than one message.
- **Emoji Mapper bot**: a manually quoted reply (the highlighted `>` excerpt
  above a reply) containing multiple premium emoji only surfaced **one** ID.
  Root cause: per the Bot API, custom_emoji entities inside a quoted excerpt
  live in `message.quote.entities` (a `TextQuote`), not in the reply's own
  `entities`. `extract_custom_emoji_ids` now also scans `quote.entities` and
  `external_reply.quote.entities`, so every premium emoji in the quote is
  listed; repeated emoji (in the quote, the reply text, or both) still collapse
  to a single ID/copy entry.

### Changed
- **Launcher (`run.ps1`)** reworked to match the FFmWiz style: a centered pink
  banner (title + full-width rule + yellow `Logging to: ...`), quiet startup
  (env/Python/ffmpeg OK lines go to the log only), cyan screen titles (no more
  black-on-cyan bar), and an ANSI 256-colour sectioned menu with sections
  lettered in order (`A`=Build, `B`=Collection, `C`=Bot), each with its own
  numbering (e.g. `A1`, `B3`, `C1`) and blank-line spacing. Removed the `q) Quit`
  row in favour of a colored `{back=0, quit=exit}` hint on every prompt. Actions
  are now step wizards (`Run-Wizard`) so **`0` steps back exactly one prompt**
  (only the first prompt returns to the menu), and `exit`/`quit` leaves from
  anywhere. Per-run UTC log under `logs\run_<UTC>.log` also records each launched
  Python command and its exit code (no secret values).
- **Emoji Mapper bot reply** reworked: sending one or more premium emoji (any
  spacing/newlines) now returns a **single collapsed (expandable) quote** of the
  **actual premium emoji** (rendered via `<tg-emoji>`) + `<code>` ID per line for
  individual tap-to-copy, plus a `copy_text` **“Copy all” inline button** for
  reliable one-click copy-all on every platform (chunked for long lists). Huge
  lists split across messages; a rejected custom emoji falls back to plain
  fallback chars.
- **`build_collection.py`** now places the **YourBrand logo as the first emoji
  of every set** built with `@YourEmojiBot`. Since Bot API 7.2 a
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
