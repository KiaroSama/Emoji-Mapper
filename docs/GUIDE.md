# Emoji Mapper — Complete Guide (0 → 100)

This is the full reference for **Emoji Mapper**. It is written so that any
person **or AI agent** can read it once and understand how the project works and
how to perform every workflow and the next steps correctly. Keep this file in
sync with the code: whenever a command, flag, file, or workflow changes, update
the matching section here.

> Proprietary software — All Rights Reserved (see [`LICENSE`](../LICENSE)).
> Telegram bot tokens and the owner id live only in `.env` / `secrets.md`
> (never committed). Never print or commit secrets.

---

## 1. What this project does

Emoji Mapper turns images/animations into **Telegram premium custom-emoji
packs**, and also **collects** premium emoji from existing packs and posts.

Two independent but related sides:

| Side | Bots | Purpose |
|------|------|---------|
| **Crypto coins** (`coins/`) | `TELEGRAM_BOT_TOKEN` (`@GodVerifyCoinEmojiMapperbot`) | the original coin-logo packs (29 packs, ~5.8k emoji) |
| **General / collector** | `GENERAL_BOT_TOKEN` (`@GodVerifyEmojiMapperbot`) | build any pack, copy packs, extract IDs |

Supported custom-emoji formats: **static** (PNG/WEBP, 100×100), **animated**
(`.tgs` = gzipped Lottie), **video** (`.webm`/VP9, ≤256 KB, ≤3 s).

---

## 2. Repository map

```
Emoji Mapper/
  run.ps1                  Windows launcher (menu). Prefer this.
  build_pack.py            core engine: upload a folder of media to emoji sets
  make_emoji_pngs.py       image -> 100x100 PNG (static)
  fetch_pack.py            collector: download a Telegram pack -> catalog
  fetch_emoji_ids.py       collector: download specific emoji by ID -> catalog
  add_media.py             collector: build emoji from local files -> catalog
  build_collection.py      collector: publish the catalog into new packs
  panel.py                 web "Curate" panel: pick which emoji to publish
  emoji_bot.py             interactive bot: extract premium-emoji IDs (tap-to-copy)
  emojikit/                shared core library
    logsetup.py            UTC file logging (logs/)
    media.py               format detect, hashing, conversions (static/video/tgs)
    catalog.py             content-addressed SQLite catalog (dedup + inclusion)
  coins/                   the crypto-coin component (see §7)
  assets/vendor/           vendored Lottie player for the panel (offline)
  tests/                   unit tests + fixtures (python -m unittest)
  docs/GUIDE.md            this file
  .env.example             config template
  collection/              (gitignored) catalog.db + media/ + manifests/ + state
  logs/  build/  input/  logos/   (gitignored) generated/working data
```

Generated/local-only (gitignored): `collection/`, `logs/`, `build/`, `input/`,
`logos/`, `*_state.json`, `*.filled.md`, `.env`, `secrets.md`,
`coins/remap_live_cache.json`, `coins/ticker_to_id.prebroken.json`.

---

## 3. Setup (one time)

```powershell
# 1. Create the virtual environment (Python 3.11 is the supported runtime)
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Configure secrets
copy .env.example .env
# edit .env and fill in tokens + owner id (see keys below)
```

`.env` keys:

```
PACK_OWNER_USER_ID=<your numeric Telegram id>   # owns every created set; press Start on each bot once
TELEGRAM_BOT_TOKEN=<coin bot token>
GENERAL_BOT_TOKEN=<general bot token>
GENERAL_BOT_USERNAME=GodVerifyEmojiMapperbot
GENERAL_BOT_NAME=GodVerify Emoji Mapper
CMC_API_KEY=<optional CoinMarketCap key, only for coins/fetch_cmc.py>
```

External tool: **ffmpeg + ffprobe** on `PATH` are required **only** for video
emoji. Install on Windows: `winget install Gyan.FFmpeg`.

Pinned deps note: `svglib<2` / `reportlab<4` are intentional — they ship the
bundled SVG rasterizer (no system cairo). These wheels exist for Python 3.11,
not 3.12, so the project targets 3.11 and CI runs on 3.11.

---

## 4. The launcher (`run.ps1`)

Right-click → *Run with PowerShell*, or `.\run.ps1`. It shows a centered banner
(pink title + full-width rule + yellow `Logging to: ...`), quietly prepares
`.venv` (env/Python/ffmpeg OK lines go to the **log only**, keeping the console
clean), then shows a **colored, sectioned** menu. Sections are lettered in order
(**A** = Build, **B** = Collection, **C** = Bot) and each has its own numbering,
so keys stay unique — type e.g. `A1`, `B3`, `C1`:

```
                              Emoji Mapper
====================================================================
Logging to: logs\run_2026-07-03_12-29-45_UTC.log

Build a single pack
  A1) Build a general emoji pack  (new bot)
  A2) Convert images to 100x100 PNGs only
  A3) Crypto-coin pack rebuild    (coin bot)

Collection (multi-format, duplicate-proof)
  B1) Collect emoji from existing packs (download)
  B2) Add media from a folder (build from scratch)
  B3) Publish the collection into new packs
  B4) Curate panel - pick which emoji to include (web)

Bot
  C1) Run the Emoji Mapper bot (premium-emoji ID extractor)

Select {quit=exit}:
```

Navigation follows the FFmWiz style: every input prompt shows a colored
`{back=0, quit=exit}` hint. Typing **0** steps back **one** prompt (e.g. from the
token prompt back to the title prompt); from the first prompt it returns to the
menu. Typing **exit** (or **quit**) leaves the launcher from anywhere. Multi-step
actions are wizards (`Run-Wizard`): each step can go back, re-ask on bad input,
or advance. Screen titles are cyan text (ANSI 256-colour; renders in Windows
Terminal / PowerShell 7). Each launched Python command and its exit code are
recorded in the launcher log.

Every run writes a UTC log to `logs\run_<YYYY-MM-DD_HH-mm-ss>_UTC.log` (startup,
prereq checks, menu selections, actions, warnings/errors, shutdown — no secret
values). Non-interactive health check (CI / scripts): `.\run.ps1 -Check`.
---

## 5. The collector model (how dedup, mapping & curation work)

The collector stores everything in a **content-addressed catalog**
(`collection/catalog.db`, SQLite, managed by `emojikit/catalog.py`). Each emoji
is one row keyed by a normalized **content hash**.

Guarantees (root-cause fixes — do not regress these):

- **No duplicates**: identical media (same normalized pixels for static; sampled
  frames for video; canonical Lottie JSON for animated) collapse to one row.
  Telegram `file_unique_id` is also remembered so an already-ingested sticker is
  never re-downloaded. **Perceptual near-duplicate merging is OFF by default**
  (it once wrongly merged distinct-but-similar emoji); enable it only with
  `--phash-threshold N` (Hamming distance) when you actually want look-alikes
  merged.
- **No blank emoji**: a blank/transparent render is never uploaded. A blank SVG
  render falls back to the raster source.
- **No mapping drift**: publishing records the *actual* upload order and marks
  each row uploaded (committed per item), so resuming after a skip/crash can
  never re-upload or scramble IDs.
- **Idempotent / resumable**: re-running fetch or publish is safe and cheap.
- **Curation**: each row has an `included` flag (default 1). The Curate panel
  toggles it; `build_collection` only publishes `included` rows.

`emojikit/media.py` cheat-sheet: `detect_format`, `content_key`,
`perceptual_hash`, `to_static_png`, `to_video_webm` (ffmpeg, VP9, ≤256 KB),
`to_animated_tgs` (Lottie→gzip), `validate_video`, `validate_tgs`.

---

## 6. Collector workflows (0 → 100)

### 6.1 Download an existing pack into the catalog

```powershell
# By pack short-name or t.me/addemoji/<name> link. Use the bot that can read it.
.venv\Scripts\python.exe fetch_pack.py <pack_or_link> [<pack2> ...] `
    --token-env GENERAL_BOT_TOKEN [--data-dir collection] [--limit N] [--phash-threshold -1]
```

Find which pack an emoji ID belongs to first (then fetch that pack):

```powershell
# getCustomEmojiStickers returns set_name + is_animated/is_video
# (see emoji_bot.enrich_labels for the call; or a one-off snippet)
```

### 6.2 Build emoji from your own files

```powershell
.venv\Scripts\python.exe add_media.py --in input\myset --emoji 😀 [--as auto|static|video|animated]
# auto: still image -> static; animated GIF/MP4/WEBM -> video; Lottie .json/.tgs -> animated
# (animated emoji are VECTOR only; a GIF/video becomes a VIDEO emoji, not animated)
```

### 6.3 Curate — pick what to publish (web panel)

```powershell
.venv\Scripts\python.exe panel.py [--data-dir collection] [--port 8765] [--no-open]
```

Dark neon panel: every emoji is a big labelled card (static=image,
video=`<video>`, animated `.tgs`=played via Lottie, lazily). All selected by
default. Click to toggle, **Shift+click** for a range. Look-alikes are ordered
adjacently. Click **Save** → writes the `included` flag to the catalog.

### 6.4 Publish the catalog into new packs

```powershell
# Preview (no upload):
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN --dry-run
# Publish for real (resumable, duplicate-proof, per-format sets):
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN [--formats static,video,animated] [--per-set 200]
```

Sets are named `<base>s<n>_by_<bot>` (static), `<base>v<n>` (video),
`<base>a<n>` (animated) — split by format for organization (since Bot API 7.2 a
set *may* mix formats, so this is a choice, not a requirement). Each finished
pack DMs the owner its `t.me/addemoji/...` link, and a per-pack manifest
(`collection/manifests/<set>.md`: name + emoji ID) is written.

---

## 7. Crypto-coin component (`coins/`)

Self-contained tool that reuses `build_pack.py` and the coin bot.

```powershell
# Logos (data): fetch + keywords
.venv\Scripts\python.exe coins\fetch_logos.py          # CoinGecko logos + keywords.csv
.venv\Scripts\python.exe coins\fetch_paprika.py        # fill from CoinPaprika
.venv\Scripts\python.exe coins\fetch_cmc.py            # fill from CoinMarketCap (needs CMC_API_KEY)
.venv\Scripts\python.exe coins\build_keywords.py       # (re)build keywords.csv from logos

# Convert logos to 100x100 emoji PNGs
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\svg --out coins\logos\emoji
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\png --out coins\logos\emoji

# Build / rebuild the packs (duplicate-proof, records upload order)
.venv\Scripts\python.exe coins\rebuild_dedup.py        # build + map + send links
.venv\Scripts\python.exe coins\rebuild_dedup.py map    # only rebuild the id map + inventory
.venv\Scripts\python.exe coins\rebuild_dedup.py links  # resend the combined links message
```

### 7.1 The ticker → custom_emoji_id map (`coins/ticker_to_id.json`)

This is the lookup table consumers use. **Always derive it from image content,
not from positions** (a historical position-based bug scrambled it):

```powershell
# Rebuild the map by matching every LIVE sticker image to its source logo:
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "PATH\to\emoji" --apply [--max-distance 200]

# Fill chain-variant / alias tickers (etharb->eth, bnbbsc->bnb, usdc.e, ...):
.venv\Scripts\python.exe coins\enhance_map.py          # strip chain suffixes -> base id
.venv\Scripts\python.exe coins\alias_map.py            # match by coin NAME -> base id
```

### 7.2 Audit, fix logos, manifests

```powershell
# Audit ALL packs for blank/duplicate stickers (downloads every sticker):
.venv\Scripts\python.exe coins\check_all_packs.py

# Review logos vs official CoinGecko art; fix ONLY confirmed-wrong tickers:
.venv\Scripts\python.exe coins\verify_logos.py --emoji-dir "PATH\to\emoji"            # report
.venv\Scripts\python.exe coins\verify_logos.py --emoji-dir "PATH\to\emoji" --fix --only sol,xrp

# Write a per-pack manifest (.md: ticker(s) + emoji id) for every pack:
.venv\Scripts\python.exe coins\write_manifests.py --out-dir "PATH\to\pack-folder"
```

> Logo similarity vs official art is **not** proof a logo is wrong (different
> icon sets differ). `verify_logos --fix` therefore only touches the tickers you
> explicitly list with `--only`.

---

## 8. The Emoji Mapper bot (`emoji_bot.py`)

Long-polling bot (run it and leave it running; only one instance at a time):

```powershell
.venv\Scripts\python.exe emoji_bot.py    # uses GENERAL_BOT_TOKEN; or run.ps1 -> C1
```

- Send it **one or more premium emoji in a row** (spaces/newlines between them
  don't matter) → it replies with a **single collapsed (expandable) quote** of
  `emoji + ID` (each line shows the **actual premium emoji** via a `<tg-emoji>`
  custom-emoji entity next to `<code>id</code>`; tap an ID to copy just that one
  on mobile), plus a **“Copy all” inline button** (`copy_text`) that copies every
  ID in one tap on any platform (desktop included). Long lists get a few
  “Copy a-b” buttons to respect the 256-char button limit. (Collapsed by height,
  so long lists show a few lines until expanded — expected, not missing data.)
- Send/forward a **post with premium emoji** → same two-format reply.
- **Add it to a channel/group** (as admin) → DMs the owner the premium-emoji IDs
  from *new* posts (Bot API cannot read past channel history).
- `/start`, `/help` show the menu (registered via `setMyCommands`).

Copy-all is reliable via the “Copy all” inline `copy_text` button; per-ID
tap-to-copy uses Telegram's native `<code>` copy (mobile). Very large lists are
split across multiple messages (each under 4096 chars).

---

## 9. Telegram limits to remember

- Custom-emoji **set cap: 200**. Formats can't be mixed in one set.
- **Animated = vector (Lottie/TGS) only.** Raster animation → **video** emoji.
- Video: VP9, 100×100, ≤3 s, 30 fps, no audio, ≤256 KB.
- A bot can `getStickerSet`/`getFile` for any pack it can see, but **cannot read
  channel history** — only posts received after it joined.
- Sending custom emoji in messages and reading their keywords back is limited:
  the Bot API exposes `custom_emoji_id`/`emoji`/`set_name` (via
  `getCustomEmojiStickers`) but **not** a sticker's search keywords.

---

## 10. Testing, CI, and Git

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"   # unit tests
.\run.ps1 -Check                                                        # env doctor
```

CI (`.github/workflows/ci.yml`, Python 3.11): installs deps + ffmpeg,
byte-compiles, import smoke test, unit tests, and an offline `build_pack`
dry-run. Run these locally before pushing.

Git: work is committed in small logical commits and pushed to `main` on
`KiaroSama/Emoji-Mapper`. Never commit `.env`, `secrets.md`, `collection/`,
`logs/`, or any token.

---

## 11. Next steps / how to extend (for a developer or AI agent)

1. **Read this guide + `README.md` first**, then the file you intend to change.
2. **Reuse the catalog + emojikit** for any new media handling. Do not add a
   parallel dedup/mapping mechanism — the catalog is the single source of truth.
3. **Preserve the guarantees in §5** (no duplicates, no blanks, no mapping
   drift, idempotent). If you add a format or ingest path, route it through
   `catalog.add(...)` with a proper `content_key` and a blank check.
4. **Publishing**: keep the per-item committed `uploaded` flag + recorded upload
   order; never reintroduce position-offset resume logic.
5. **Curation**: respect the `included` flag in any new publish path.
6. **UI changes** (panel): keep the dark neon-blue style, Inter font, visible
   focus, `prefers-reduced-motion`, lazy media (IntersectionObserver) so large
   catalogs stay fast. Verify in a real browser before claiming done.
7. **External libs/APIs**: check current docs before coding (the Telegram Bot
   API and any JS player evolve). Keep `svglib<2`/`reportlab<4` unless you also
   solve the SVG backend on the target Python.
8. **Always**: add/maintain tests, run the unit suite + a real run, then commit
   and **push to keep GitHub in sync**, and **update this guide** with any new
   command 0 → 100.

---

*Keep this document synchronized with the code. When you add or change a
command, add it here in full.*
---

## 12. Full CLI reference

Every entry point, every flag, with defaults and examples. All commands assume
you run them from the project root with the venv Python
(`.venv\Scripts\python.exe`). On macOS/Linux use `.venv/bin/python`.

### 12.1 `make_emoji_pngs.py` — image → 100×100 PNG

| Flag | Default | Meaning |
|------|---------|---------|
| `--in <dir>` | *(empty)* | General mode: source folder of mixed images. |
| `--out <dir>` | `<in>_emoji` | Output folder for the 100×100 PNGs. |
| `--limit <n>` | `0` (all) | Convert at most N images this run. |

Behaviour:

- **General mode** (when `--in` is given): converts every `.svg .png .jpg .jpeg
  .webp .gif .bmp .apng` in `--in` to `<name>.png` (100×100, transparent) in
  `--out`. Skips files already converted (idempotent), and records a marker so a
  hung SVG is blacklisted on the next run (`.svg_skip.txt`, `.svg_cur`).
- **Legacy coin mode** (no `--in`): reads `logos/svg/*.svg` then `logos/png/*.png`
  and writes `logos/emoji/<ticker>.png`.
- **Blank guard**: if an SVG renders blank (e.g. an unsupported gradient), it is
  **not** saved — the loop falls back to the raster `png/` source; a blank raster
  is skipped too. No blank emoji is ever produced.

Examples:

```powershell
.venv\Scripts\python.exe make_emoji_pngs.py --in input\myset --out build\myset
.venv\Scripts\python.exe make_emoji_pngs.py --in input\myset --limit 50
.venv\Scripts\python.exe make_emoji_pngs.py            # legacy coin mode
```

### 12.2 `build_pack.py` — upload a folder of PNGs to emoji sets

| Flag | Default | Meaning |
|------|---------|---------|
| `--base` | *(required)* | Set-name base (letters/digits/`_`). |
| `--title` | *(required)* | Human-readable set title. |
| `--source-dir` | `logos/emoji` | Folder of 100×100 PNGs to upload. |
| `--token-env` | `TELEGRAM_BOT_TOKEN` | Env var holding the bot token. |
| `--keywords` | `auto` | keywords CSV; `auto` = coin `keywords.csv` only for the default source. |
| `--emoji` | `🪙` | Associated standard emoji. |
| `--user-id` | `PACK_OWNER_USER_ID` | Numeric owner id. |
| `--per-set` | `400` | Emojis per set (Telegram hard cap is **200**; keep ≤200). |
| `--limit` / `--start` | `0` / `0` | Process a slice of the source. |
| `--state` | `state_<base>.json` | Resume file (per pack, never clobbered). |
| `--dry-run` | off | Validate inputs without calling Telegram. |

Resumable: progress is saved to `state_<base>.json`; an interrupted/flood-limited
run continues without recreating existing sets. Use `--dry-run` first.

```powershell
.venv\Scripts\python.exe build_pack.py --base myset --title "My Emojis" `
    --source-dir build\myset --token-env GENERAL_BOT_TOKEN --emoji 😀 --dry-run
```

### 12.3 `fetch_pack.py` — download a Telegram pack into the catalog

| Flag | Default | Meaning |
|------|---------|---------|
| `packs...` | *(required)* | One or more pack short-names or `t.me/addemoji/<name>` links. |
| `--token-env` | `GENERAL_BOT_TOKEN` | Bot token env var. |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--phash-threshold` | `-1` (off) | Hamming distance for near-dup merging; `-1` keeps look-alikes. |
| `--limit <n>` | `0` (all) | Max new items per pack. |

Re-running is cheap: stickers whose `file_unique_id` was already ingested are
skipped without downloading; identical media collapse to one catalog row.

### 12.3b `fetch_emoji_ids.py` — download *specific* emoji by ID (not whole packs)

Downloads only the individual premium custom-emoji you name — e.g. the
`premium-id:<n>` entries inside bot inventory files — and nothing else from
their packs. Only **real entry lines** that start with `premium-id:` are taken;
example/prose mentions like `(e.g. premium-id: 123)` are skipped so example IDs
are never fetched by mistake. IDs are then **de-duplicated** so each real emoji
is fetched at most once, resolved via `getCustomEmojiStickers` (batched,
≤200/call), downloaded and content-hashed into the same catalog.

| Flag | Default | Meaning |
|------|---------|---------|
| `--ids-file <path>` | *(empty)* | File containing `premium-id:<n>` lines or bare IDs. Repeatable. |
| `--id <n>` | *(empty)* | A single custom-emoji ID. Repeatable. |
| `--token-env` | `GENERAL_BOT_TOKEN` | Bot token env var (any bot can resolve IDs). |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--phash-threshold` | `-1` (off) | Near-dup merge threshold; `-1` keeps look-alikes. |

Two levels of de-duplication protect you: **ID-level** (repeated IDs across
files fetched once) and **content-level** (two different IDs pointing at the
same media collapse to one catalog row). It also reports **within-file
duplicates** (the same real entry repeated inside one file) separately from
cross-file duplicates. The run reports
`unique_ids / new / dedup / failed / missing`; `missing` counts IDs Telegram
could no longer resolve. Each row keeps its origin in `keywords`
(`premium-id:<n>`). The extraction/dedup logic is covered by
`tests/test_fetch_emoji_ids.py` (example lines, trailing labels, bullets,
within-file and cross-file duplicates, bare-ID lists).

Example — pull only the emoji referenced by four bot inventory files:

```powershell
.venv\Scripts\python.exe fetch_emoji_ids.py `
  --ids-file "...\GV Swap bot\bot-emoji-inventory-user.md" `
  --ids-file "...\GV Swap bot\bot-emoji-inventory-admin.md" `
  --ids-file "...\GodVerify Payment Bot\bot-emoji-inventory-admin.md" `
  --ids-file "...\GodVerify Payment Bot\bot-emoji-inventory-user.md"
# -> collected 240 real id occurrences -> 101 unique (83 ids duplicated across files)
# -> Done. unique_ids=101 new=100 dedup=1 failed=0 missing=0
```

### 12.4 `add_media.py` — build emoji from local files into the catalog

| Flag | Default | Meaning |
|------|---------|---------|
| `files...` | *(optional)* | Individual source files. |
| `--in <dir>` | *(empty)* | Folder of source files. |
| `--as` | `auto` | `auto`/`static`/`video`/`animated` target format. |
| `--emoji` | `😀` | Associated standard emoji. |
| `--keywords` | *(empty)* | Comma-separated extra keywords. |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--phash-threshold` | `-1` (off) | Near-dup merge threshold. |

Auto format: still image → static; animated GIF/APNG/MP4/WEBM/MOV → **video**
(ffmpeg); Lottie `.json`/`.tgs` → **animated**. Remember: a raster animation
cannot become an *animated* emoji (those are vector-only) — it becomes a *video*
emoji.

### 12.5 `build_collection.py` — publish the catalog into new packs

| Flag | Default | Meaning |
|------|---------|---------|
| `--base` | *(required)* | Set-name base (letters/digits only). |
| `--title` | *(required)* | Human-readable title. |
| `--token-env` | `GENERAL_BOT_TOKEN` | Bot token env var. |
| `--user-id` | `PACK_OWNER_USER_ID` | Numeric owner id. |
| `--emoji` | `😀` | Fallback associated emoji. |
| `--formats` | `static,video,animated` | Which formats to publish, in order. |
| `--per-set` | `200` | Emojis per set. |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--brand-logo` | God Verify logo PNG | First-emoji brand logo (Emoji Mapper bot only). |
| `--no-brand-logo` | off | Disable the mandatory first-emoji logo. |
| `--dry-run` | off | Show the plan without uploading. |

Only **included** (panel-selected), not-yet-uploaded, non-skipped items are
published. Per-format sets, drift-proof resume, per-pack manifests.

**Brand logo (first emoji of every set).** When publishing with the
`@GodVerifyEmojiMapperbot` bot, the God Verify logo is inserted as the **first
emoji of every set** (`--brand-logo`, default
`F:\documents\My Logo\God Verify\God Verify Emoji Logo.png`). Since Bot API 7.2
(March 2024) a single custom-emoji set may contain **mixed formats**, so the
logo is always a **static** 100x100 PNG and leads a static, video *or* animated
set alike (verified live). The `@GodVerifyCoinEmojiMapperbot` coin bot is exempt.
Disable with `--no-brand-logo`. The logo occupies position 0, so item
`custom_emoji_id`s are read from position 1 onward (handled automatically).

### 12.6 `panel.py` — curate web panel

| Flag | Default | Meaning |
|------|---------|---------|
| `--data-dir` | `collection` | Catalog/media directory. |
| `--port` | `8765` | Local port. |
| `--no-open` | off | Don't auto-open the browser. |

### 12.7 `emoji_bot.py` — premium-emoji ID extractor bot

No flags. Uses `GENERAL_BOT_TOKEN` + `PACK_OWNER_USER_ID` from `.env`. One
instance at a time (two pollers cause Telegram 409 Conflict). Replies with two
collapsed quotes (emoji+ID, and IDs-only) using tap-to-copy `<code>` (see §8/§19).

### 12.8 `coins/` commands

| Command | Purpose |
|---------|---------|
| `coins\fetch_logos.py` | Download coin logos (CoinGecko) + write `keywords.csv`. |
| `coins\fetch_paprika.py` | Fill remaining coins from CoinPaprika. |
| `coins\fetch_cmc.py` | Fill remaining coins from CoinMarketCap (needs `CMC_API_KEY`). |
| `coins\build_keywords.py` | (Re)build `keywords.csv` from logos on disk. |
| `coins\rebuild_dedup.py [map\|links\|build]` | Duplicate-proof rebuild; `map` re-derives the id map; `links` resends links; `build` uploads only. |
| `coins\rebuild_packs.py [map]` | Older non-dedup rebuild (kept for reference). |
| `coins\remap_ids.py --emoji-dir DIR [--apply] [--max-distance N]` | Rebuild `ticker_to_id.json` by image content (drift-proof). |
| `coins\verify_logos.py --emoji-dir DIR [--fix --only a,b]` | Review logos vs official; fix only listed tickers. |
| `coins\check_all_packs.py` | Audit all packs for blank/duplicate stickers. |
| `coins\write_manifests.py --out-dir DIR` | Write per-pack manifest `.md` files. |
| `coins\enhance_map.py` | Map chain-suffixed tickers (e.g. `bnbbsc`) to the base id. |
| `coins\alias_map.py` | Map tickers to a base id by matching coin name. |
---

## 13. The catalog database (`collection/catalog.db`)

SQLite, created/managed by `emojikit/catalog.py`. Three tables:

### 13.1 `items` — one row per distinct emoji

| Column | Type | Meaning |
|--------|------|---------|
| `content_key` | TEXT PK | Normalized content hash, prefixed by format: `s:` static, `v:` video, `a:` animated, `r:` raw fallback. |
| `format` | TEXT | `static` / `video` / `animated`. |
| `file_path` | TEXT | Absolute path to the stored media file. |
| `emojis` | TEXT (JSON) | Associated standard emoji(s), e.g. `["🪙"]`. |
| `keywords` | TEXT (JSON) | Search keywords / labels. |
| `sources` | TEXT (JSON) | Where it came from, e.g. `["RMaccs"]` or `["local:foo.png"]`. |
| `phash` | INTEGER | 64-bit dHash stored as **signed** 64-bit (two's complement) to avoid SQLite overflow; restored to unsigned on read. |
| `custom_emoji_id` | TEXT | Live Telegram id after upload (else NULL). |
| `uploaded` | INTEGER | `1` once published (committed per item → crash-safe dedup). |
| `included` | INTEGER | `1` = will be published (panel selection); `0` = excluded. |
| `created_utc` | TEXT | `YYYY-MM-DD HH:MM:SS UTC`. |

Indexes: `idx_items_format`, `idx_items_uploaded`.

### 13.2 `seen_files` — fast pre-dedup

| Column | Type | Meaning |
|--------|------|---------|
| `file_unique_id` | TEXT PK | Telegram's stable per-sticker id. |
| `content_key` | TEXT | The catalog row it maps to. |

If a sticker's `file_unique_id` is already here, `fetch_pack` skips the download
entirely and just merges labels.

### 13.3 `meta` — key/value (e.g. `schema_version`).

### 13.4 Why phash is stored signed

A dHash is an **unsigned** 64-bit integer. SQLite integers are signed 64-bit, so
values ≥ 2⁶³ raised *"Python int too large to convert to SQLite INTEGER"* and
dropped rows. The catalog converts: `to_db = x - 2⁶⁴ if x ≥ 2⁶³ else x`, and
`from_db = v & (2⁶⁴-1)`. Hamming distance is computed on the restored unsigned
values. (Regression test: `tests/test_catalog.py::test_large_phash_64bit`.)

### 13.5 Inspecting the catalog manually

```powershell
.venv\Scripts\python.exe -c "import sqlite3;d=sqlite3.connect('collection/catalog.db');
print(d.execute('SELECT format,COUNT(*),SUM(uploaded),SUM(included) FROM items GROUP BY format').fetchall())"
```

---

## 14. `emojikit` library API

### 14.1 `emojikit.media`

Constants: `SIZE=100`, `TGS_MAX_BYTES=65536`, `WEBM_MAX_BYTES=262144`,
`WEBM_MAX_SECONDS=3.0`, `WEBM_FPS=30`.

| Function | Returns | Notes |
|----------|---------|-------|
| `detect_format(path)` | `"static"\|"animated"\|"video"\|"unknown"` | By magic bytes, then extension. |
| `detect_format_bytes(head)` | same | From leading bytes only. |
| `telegram_sticker_format(sticker)` | format | From `is_animated`/`is_video`. |
| `ext_for_format(fmt)` | `.png`/`.tgs`/`.webm` | Canonical extension. |
| `media_extension(path, fmt)` | str | Refines static into `.png` vs `.webp`. |
| `fit_100(img)` | PIL.Image | Trim transparent borders, center on 100×100 RGBA. |
| `to_static_png(src, out)` | Path | Any image (SVG via svglib) → 100×100 PNG. |
| `to_video_webm(src, out)` | Path | ffmpeg → VP9 WEBM, 100×100, ≤3 s, transparent-padded; CRF escalates until ≤256 KB. |
| `probe_video(path)` | `VideoInfo(width,height,duration,codec)` | via ffprobe. |
| `validate_video(path)` | raises on violation | dims/duration/codec/size checks. |
| `to_animated_tgs(src, out)` | Path | Lottie `.json`/`.tgs` → valid 100×100 `.tgs` (gzip). |
| `validate_tgs(path)` | raises | ≤64 KB + required Lottie keys. |
| `content_key(path, fmt)` | str | Dedup primary key (see §16). |
| `perceptual_hash(path, fmt)` | int\|None | 64-bit dHash for static/video; None for animated. |
| `hamming(a, b)` | int | Bit difference between two hashes. |

The ffmpeg filter used for video:
`fps=30,scale=100:100:force_original_aspect_ratio=decrease:flags=lanczos,format=rgba,pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=yuva420p`,
encoded with `libvpx-vp9 -pix_fmt yuva420p -auto-alt-ref 0`.

### 14.2 `emojikit.catalog.Catalog`

```python
from emojikit.catalog import Catalog
with Catalog("collection/catalog.db", phash_threshold=-1) as cat:
    cat.add(content_key=..., fmt="static", file_path=..., emojis=[...],
            keywords=[...], source="RMaccs", phash=..., file_unique_id=...)
    cat.seen_file_unique_id(fuid)        # -> content_key | None
    cat.merge_labels(key, emojis=..., keywords=..., source=..., file_unique_id=...)
    cat.pending(fmt=None)                # not-uploaded AND included, deterministic order
    cat.all_items(fmt=None)              # every row (for the panel)
    cat.mark_uploaded(key, custom_emoji_id)
    cat.set_inclusion(excluded_keys)     # included=0 for those, 1 for the rest
    cat.get(key)                         # -> Item | None
    cat.stats()                          # {fmt: {total, uploaded, pending}}
```

`add(...)` returns `(canonical_key, is_new)`. With `phash_threshold >= 0` it also
merges perceptual near-duplicates; with `-1` (default) only exact content +
`file_unique_id` dedup happens.

### 14.3 `emojikit.logsetup` (advanced logging)

`setup_logging(name, *, console_level=INFO, file_level=DEBUG, json_sidecar=False,
color=None)` configures a console handler plus a fresh UTC file log under
`logs/`, named `<name>_YYYY-MM-DD_HH-mm-ss_UTC_<run_id>.log`. It is idempotent
per process and returns the root logger. Capabilities:

- **Per-run id** — an 8-hex id stamped on every file line (`get_run_id()`).
- **Automatic secret redaction** — known secret *values* (auto-registered from
  `TELEGRAM_BOT_TOKEN`, `GENERAL_BOT_TOKEN`, `CMC_API_KEY`, …) and token-shaped
  strings/`/bot<token>/` URLs are masked in **every** record, including
  exception tracebacks and the JSON sidecar. Content hashes (`s:...`) and numeric
  ids are **not** redacted. Register extra secrets with `register_secret(value)`.
- **Rich file format** — `[UTC] [LEVEL] [run_id] [logger] module:line message`;
  concise (optionally ANSI-colored on TTY) console format.
- **JSONL sidecar** — pass `json_sidecar=True` to also write `*.jsonl`.
- **Uncaught-exception capture** — `sys.excepthook` + threading hook log full
  tracebacks as CRITICAL.
- **Quiet third parties** — `urllib3`/`requests`/`PIL` turned down;
  `logging.captureWarnings(True)`.
- **End-of-run summary** (atexit) — `run <id> finished in N.NNs | warnings=… errors=… critical=…`.

Helpers for scripts:

```python
from emojikit.logsetup import setup_logging, log_duration, logcall, get_run_id, redact
setup_logging("myscript", json_sidecar=True)
with log_duration("download pack"):     # logs start/finish + elapsed (+ failure)
    ...
@logcall                                 # logs entry/exit/duration/exceptions
def work(...): ...
```

`redact(text)` is also exported for masking before any manual print.

### 14.4 `build_pack.Telegram`

Thin Bot API client (used everywhere). Key methods: `get_me`, `send_message`,
`get_sticker_set`, `download_file`, `create_emoji_set`/`add_emoji` (format-aware,
for static/animated/video), and the legacy static `create_set`/`add_sticker`.
`_call(method, data=, files=)` handles flood waits (`retry_after`) and the
~2-minute `STICKERSET_INVALID` name-release delay automatically.
---

## 15. Media formats deep-dive

### 15.1 Static (`static`)

- File: PNG or WEBP, **exactly 100×100**, RGBA (transparent background).
- Built by `make_emoji_pngs._fit_100` / `media.to_static_png`: trim fully
  transparent borders, scale to fit 100×100 with LANCZOS, center on a
  transparent canvas. SVG sources are rasterized via svglib + reportlab's
  bundled renderPM (that is why `reportlab<4` is pinned).
- Telegram stores static stickers as WEBP; when downloaded they come back as
  `.webp` (the panel renders them directly in `<img>`).

### 15.2 Animated (`animated`)

- File: `.tgs` = **gzip-compressed Lottie JSON** (vector animation).
- Hard cap **64 KB**; emoji canvas 100×100; ≤3 s; up to 60 fps.
- **Vector only.** You cannot turn a GIF/MP4 into an animated emoji — that path
  produces a *video* emoji. `media.to_animated_tgs` only packages/validates an
  existing Lottie (`.json` or `.tgs`): load → (rescale layers if the canvas
  isn't 100×100) → `json.dumps` minified → gzip with `mtime=0` (deterministic) →
  `validate_tgs` (size + required keys `v, fr, ip, op, layers`).
- In the panel, `.tgs` is gunzipped server-side (`/lottie/<key>`) and played by
  the vendored Lottie SVG player (lazily; only on-screen ones run).

### 15.3 Video (`video`)

- File: `.webm` with **VP9** codec, 100×100, ≤3 s, 30 fps, **no audio**, **≤256 KB**.
- Built by `media.to_video_webm` with ffmpeg (see §14.1 for the exact filter).
  CRF escalates `32 → 40 → 48 → 56 → 63` until the file fits 256 KB.
- `validate_video` checks dimensions, duration, codec, and size via ffprobe.
- Requires `ffmpeg`/`ffprobe` on PATH. The panel previews video with `<video>`.

### 15.4 Detection summary

| Leading bytes | Format |
|---------------|--------|
| `1F 8B` (gzip) | animated (`.tgs`) |
| `1A 45 DF A3` (EBML) | video (`.webm`) |
| `89 50 4E 47` (PNG) or `GIF8` | static |
| `RIFF....WEBP` | static (WEBP) |

From the Bot API, prefer the sticker object's `is_animated` / `is_video` flags.

---

## 16. Deduplication deep-dive

### 16.1 The content key

`media.content_key(path, fmt)` produces the catalog primary key:

- **static** → `"s:" + sha256(image.convert(RGBA).resize(64×64, LANCZOS).tobytes())[:32]`.
  Two byte-different files that look identical after normalization collapse;
  truly distinct images get distinct keys.
- **video** → `"v:" + sha256(<sampled frames>)`. ffmpeg samples `fps=10` over
  ≤3 s at 64×64 RGBA; visually identical videos match regardless of container.
- **animated** → `"a:" + sha256(<canonical Lottie JSON>)`. The `.tgs` is
  gunzipped, parsed, re-serialized with sorted keys; re-gzipped copies match.
- unknown → `"r:" + sha256(raw bytes)`.

### 16.2 Perceptual near-duplicate (opt-in)

`media.perceptual_hash` is a 64-bit **dHash** (difference hash): downscale to
9×8 grayscale, compare horizontally adjacent pixels → 64 bits. `hamming(a,b)`
counts differing bits.

Near-dup merging is controlled by `Catalog.phash_threshold`:

- `-1` (**default**) → OFF. Only exact content + `file_unique_id` dedup. This is
  what you want when faithfully copying a pack (distinct-but-similar emoji are
  kept). *History: the old default of 5 wrongly merged 80 distinct RMaccs emoji
  down to 70 — see §27.*
- `>= 0` → merge rows whose dHash is within that Hamming distance. Use only when
  you intentionally want look-alikes collapsed.

### 16.3 Empirical guarantee

`coins/check_all_packs.py` downloads **every** live sticker and reports BLANK
(≤8 visible pixels) and DUPLICATE (pixel-identical distinct ids) groups. The 29
coin packs audited **0 blank / 0 duplicate** across 5,791 stickers.

---

## 17. The ticker → id map, drift, and the content-based fix

### 17.1 What went wrong historically

The coin packs were built by uploading a frozen plan in order. A handful of plan
items **failed to upload and were skipped**. The id map was then derived by
*position* (`live[i] = plan[i]`), but every skip shifted later positions — so
from the first skip onward, `ticker_to_id.json` pointed many tickers at the
**wrong** sticker (e.g. USDT showed another coin's logo).

### 17.2 The fix: content-based mapping (`coins/remap_ids.py`)

Ignore positions entirely. Download every live sticker once, compute a small
16×16 RGB **signature**, and match each local source logo (`<ticker>.png`) to the
live sticker whose signature is nearest (L2 via a chunked Gram matrix). Drop
matches above `--max-distance` (coins never uploaded). This rebuilt 4,121 of
5,862 entries correctly and is the canonical way to (re)build the map.

```powershell
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "F:\...\emoji" --max-distance 200 --apply
```

A resumable cache (`coins/remap_live_cache.json`, gitignored) avoids
re-downloading. The previous map is backed up to `ticker_to_id.prebroken.json`.

### 17.3 Preventing drift in new builds

Both `coins/rebuild_dedup.py` and `build_collection.py` now record the **actual
upload order** and mark each item uploaded as they go, then assign real
`custom_emoji_id`s from that recorded order — never from positions. `verify_logos
--fix` is the safe tool for individual wrong logos.

### 17.4 Chain-variant / alias tickers

Many inventory tickers are the same coin on another chain (`etharb`, `bnbbsc`,
`maticusdce`, `sysevm`, `stzent`, …) and have no own logo file. `enhance_map.py`
(strip chain suffix → base) and `alias_map.py` (match by coin name) point them at
the base coin's id. This filled the NOWPayments inventory to 354/354.

---

## 18. Step-by-step worked examples

### 18.1 Copy a foreign pack faithfully, curate, republish

```powershell
# 1) Download the source pack (all distinct stickers kept; near-dup merge off):
.venv\Scripts\python.exe fetch_pack.py https://t.me/addemoji/SomePack_by_bot --token-env GENERAL_BOT_TOKEN
#    -> "Pack SomePack (...): 80 stickers -> new=80 dedup=0 failed=0"

# 2) Open the curate panel and untick anything you don't want, then Save:
.venv\Scripts\python.exe panel.py
#    (browser opens http://127.0.0.1:8765/ ; click cards / Shift+click ranges / Save)

# 3) Preview the publish plan:
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN --dry-run
#    -> "static: 76 emoji -> 1 set(s) named mypacks1_by_<bot> ..."

# 4) Publish (resumable). Each finished pack DMs you its addemoji link:
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN
#    -> manifests written to collection/manifests/mypacks1_by_<bot>.md
```

### 18.2 Find which pack an emoji id belongs to, then download it

```powershell
# Resolve id -> set_name (uses getCustomEmojiStickers), then fetch that set:
.venv\Scripts\python.exe -c "import os,sys,json;sys.path.insert(0,'.');from build_pack import Telegram,load_env;load_env();tg=Telegram(os.environ['GENERAL_BOT_TOKEN']);print(tg._call('getCustomEmojiStickers',data={'custom_emoji_ids':json.dumps(['<ID>'])})[0]['set_name'])"
.venv\Scripts\python.exe fetch_pack.py <set_name> --token-env GENERAL_BOT_TOKEN
```

### 18.3 Build a video emoji pack from GIFs

```powershell
# ffmpeg must be installed. GIFs become VP9 .webm video emoji.
.venv\Scripts\python.exe add_media.py --in input\my-gifs --emoji 🔥
.venv\Scripts\python.exe build_collection.py --base myvid --title "My Animations" `
    --token-env GENERAL_BOT_TOKEN --formats video
```

### 18.4 Rebuild the coin id map after any pack change

```powershell
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "F:\...\emoji" --max-distance 200 --apply
.venv\Scripts\python.exe coins\enhance_map.py
.venv\Scripts\python.exe coins\alias_map.py
.venv\Scripts\python.exe coins\write_manifests.py --out-dir "F:\...\@GodVerify Crypto Emoji"
```
---

## 19. The Emoji Mapper bot internals (`emoji_bot.py`)

Long-polling loop: `getUpdates(offset, timeout=50, allowed_updates=[message,
channel_post, edited_channel_post, my_chat_member])`. Each update is dispatched
to `handle_update`. Errors in one update are caught and logged so the bot never
dies on a single bad message.

Pure, unit-tested helpers:

- `extract_custom_emoji_ids(message)` — ordered, de-duplicated `custom_emoji_id`s
  from `entities` + `caption_entities`.
- `build_payloads(ids, labels, rich=True)` — returns a **list of
  `(html_text, inline_keyboard)`** payloads. Each message has a single collapsed
  `<blockquote expandable>` where every line is
  `<tg-emoji emoji-id=id>fallback</tg-emoji> <code>id</code>` when `rich`
  (renders the **real premium emoji**); each `<code>` is tap-to-copy (mobile).
  Copy-all is provided by a `copy_text` **“Copy all” inline button**
  (`_copy_keyboard`), chunked into “Copy a-b” buttons when the 256-char
  `copy_text` limit is exceeded.
  `_batch_ids` splits the list (mode-independent, ~110 chars/id) so every message
  stays under `MSG_MAX` (3500) chars and rich/plain renders align 1:1;
  multi-message replies are labelled "part i/n". `send_reply` sends the rich
  version and, if a message is rejected (a custom emoji the bot can't render),
  automatically re-sends that message with `rich=False` (fallback chars only).
- `enrich_labels(tg, ids)` — `getCustomEmojiStickers` (≤200/call) → `id → emoji char`.

Behaviour by chat type:

| Incoming | Action |
|----------|--------|
| `/start`, `/help`, `/menu` (private) | Send the help/menu text. |
| Private message with premium emoji | Reply with the two-format collapsed quotes + Copy-all button. |
| Group message with premium emoji | DM the **owner** the IDs (header = group title). |
| Channel post with premium emoji | DM the **owner** the IDs (header = channel title). |

"Tap to copy" = Telegram `InlineKeyboardButton.copy_text` (Bot API 9.0); no
callback is needed — the client copies locally. `setMyCommands` registers
`/start` and `/help` in the bot's menu.

Operational notes: run exactly one instance (two concurrent `getUpdates` cause
**409 Conflict**). For groups the bot needs admin or privacy-mode off to see
messages; for channels it must be an admin to receive `channel_post`.

---

## 20. The Curate panel internals (`panel.py`)

A `ThreadingHTTPServer` on `127.0.0.1`. Routes:

| Route | Response |
|-------|----------|
| `GET /` | The single-page HTML (items embedded as JSON). |
| `GET /img/<key>` | The media bytes (webp/png/webm) with correct MIME. |
| `GET /lottie/<key>` | The `.tgs` gunzipped to Lottie **JSON** (for the player). |
| `GET /static/<file>` | Vendored assets (the Lottie player), traversal-guarded. |
| `POST /api/save` | Body `{"excluded":[keys]}` → `catalog.set_inclusion(...)`. |

Ordering: `order_by_similarity` groups items by format (static, then video, then
animated) and within each runs a greedy nearest-neighbour walk on the perceptual
hash so look-alikes are adjacent. Items without a hash (animated) keep content
order.

Front-end:

- Dark OLED theme, neon-blue accent (`#22d3ee`), Inter font, big 108-px cards
  with a label and a format badge. Thumbnails sit on a **dark-slate contrast
  checkerboard** (`#828c9a`/`#464e5a`, matching the dark theme) so black,
  hollow-center, and faint/low-opacity emoji are all visible by
  default, plus a **Backdrop switch** (Checker → Light → Dark → Gray, persisted
  in `localStorage`) to inspect tricky emoji on any background.
- All selected by default. Click toggles; **Shift+click** toggles a range.
  Header buttons: Select all / Deselect all / Invert / Save.
- Static → `<img loading="lazy">`; video → `<video autoplay muted loop>`;
  animated → a Lottie container loaded **lazily** by an `IntersectionObserver`
  (250-px root margin) and **destroyed** when scrolled off-screen, so a catalog
  with hundreds of animations stays fast (verified: ~60 active near view, 0 when
  off-screen).
- `prefers-reduced-motion` is respected (animated shows the first frame stopped).

The Lottie player is **vendored** at `assets/vendor/lottie_svg.min.js` (no
runtime CDN), so the panel works offline.

---

## 21. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `GENERAL_BOT_TOKEN not set` / `getMe failed: Not Found` | Token missing/invalid in `.env`, or a stale shell env var shadowing it. | Put a valid token in `.env`; in PowerShell clear a leftover var: `Remove-Item Env:\GENERAL_BOT_TOKEN`. |
| `ffmpeg not found` | ffmpeg/ffprobe not on PATH. | `winget install Gyan.FFmpeg` (only needed for video). |
| `cannot find ft2build.h` / reportlab build fails | Installing `reportlab<4` from source on Python 3.12 (no wheel). | Use **Python 3.11** (the supported runtime). |
| `Python int too large to convert to SQLite INTEGER` | Old catalog code stored an unsigned 64-bit phash. | Fixed in `emojikit/catalog.py` (signed storage); pull latest. |
| Fetch reports `dedup` on a pack with no real duplicates | Near-dup merging was on. | It's **off by default** now (`--phash-threshold -1`); pull latest or pass `-1`. |
| Panel images 404 | `/img/<key>` not URL-decoded. | Fixed; pull latest. |
| Bot replies nothing in a group | Privacy mode on / not admin. | Make the bot admin or disable privacy mode in BotFather. |
| Bot stops with **409 Conflict** | Two `getUpdates` consumers. | Run only one `emoji_bot.py` instance. |
| `STICKERSET_INVALID` right after deleting a set | Telegram locks a freed set name ~2 min. | The client auto-waits and retries; just let it run. |
| `flood wait Ns` | Telegram rate limit. | The client honors `retry_after` automatically; let it continue. |
| Animated card shows ▶ but never plays | Bad/non-standard `.tgs`, or JS blocked. | Other cards still work; that single TGS just won't render. |
| Wrong logo for a coin | Source logo is a different token with the same ticker. | `coins\verify_logos.py --fix --only <ticker>` with the correct image. |
| Scrambled ticker→id map | Position-based mapping (legacy). | Rebuild with `coins\remap_ids.py ... --apply`. |
| CI red on push | A check failed (install/compile/import/test/dry-run). | `gh run view <id> --log-failed`; fix; the matrix is Python 3.11 only. |

---

## 22. FAQ

**Q: Can I turn a GIF into an *animated* emoji?**
No. Animated emoji are vector (Lottie). A GIF/MP4/WEBM becomes a **video** emoji.

**Q: Does fetching the same pack twice cost bandwidth?**
No. `file_unique_id` is remembered; already-seen stickers are skipped.

**Q: How do I keep distinct-but-similar emoji?**
Leave near-dup merging off (default). Only exact-content duplicates merge.

**Q: Can the bot scan a whole channel's history?**
No. The Bot API only delivers posts received after the bot joined.

**Q: Where do the share links go?**
`build_collection` and the coin tools DM the owner (`PACK_OWNER_USER_ID`) each
finished pack's `t.me/addemoji/...` link.

**Q: Is my data uploaded anywhere?**
Only to Telegram, via your bots. `collection/`, `logs/`, `.env`, `secrets.md`
stay local and are gitignored.

**Q: Can one set hold both static and animated emoji?**
Yes — since Bot API 7.2 (March 2024) a single custom-emoji set may mix static,
video and animated stickers (each `InputSticker` carries its own `format`;
verified live). `build_collection` still splits into `s`/`v`/`a` sets by default
for organization, but the static brand logo is added as the first emoji of every
set regardless of the set's format.

---

## 23. Glossary

- **custom_emoji_id** — Telegram's id for a premium emoji; what consumers store.
- **file_unique_id** — stable per-sticker id used for fast pre-dedup.
- **content_key** — our normalized content hash; the catalog primary key.
- **dHash / perceptual hash** — 64-bit fingerprint of an image's gradient; close
  Hamming distance ≈ visually similar.
- **TGS** — gzip-compressed Lottie JSON = an animated sticker/emoji.
- **set / pack** — a Telegram sticker set (≤200 custom emoji).
- **plan** — the frozen, ordered list of items to upload (resume is deterministic).
- **included flag** — per-item publish toggle set by the Curate panel.
- **drift** — when live sticker order ≠ assumed order, scrambling a position map.

---

## 24. Telegram Bot API methods used

| Method | Where | Purpose |
|--------|-------|---------|
| `getMe` | everywhere | Auth check + bot username. |
| `getStickerSet` | fetch/audit/build | List a set's stickers. |
| `getFile` + file download | fetch/audit/remap | Download sticker bytes. |
| `uploadStickerFile` | build_pack | Upload a sticker file. |
| `createNewStickerSet` | build | Create a new custom-emoji set. |
| `addStickerToSet` | build | Append to a set. |
| `replaceStickerInSet` | verify_logos / fixes | Swap a wrong sticker in place. |
| `deleteStickerSet` | rebuild | Remove old packs before a clean rebuild. |
| `getCustomEmojiStickers` | bot / resolve | id → emoji char + `set_name` (≤200/call). |
| `sendMessage` (+ `entities`, `reply_markup`) | bot / links | Send IDs, copy buttons, links. |
| `deleteMessage` | bot housekeeping | Remove old bot messages. |
| `setMyCommands` | bot | Register `/start`, `/help`. |
| `getUpdates` | bot | Long polling. |

Tap-to-copy uses Telegram's native `<code>` entity copy (works for a single id
and for a whole multi-line `<code>` block); collapsed quotes use
`<blockquote expandable>`. Emoji sets cap at 200 stickers (a brand logo, when
added, counts as the first of those 200).

---

## 25. Data files reference

| Path | Tracked? | What |
|------|----------|------|
| `.env` | no | Tokens + owner id. |
| `secrets.md` | no | Local secret registry. |
| `collection/catalog.db` | no | The content-addressed catalog. |
| `collection/media/{static,video,animated}/` | no | Downloaded/built media. |
| `collection/manifests/<set>.md` | no | Per-pack manifest (name + id). |
| `collection/publish_<base>.json` | no | Publish state (sets, sent links, keys, skipped). |
| `collection/publish_plan_<base>.json` | no | Frozen per-format upload plan. |
| `coins/ticker_to_id.json` | **yes** | Canonical ticker → custom_emoji_id map. |
| `coins/keywords.csv` | **yes** | ticker → name/keywords. |
| `coins/currency-emoji-inventory.md` | **yes** | Inventory source. |
| `coins/currency-emoji-inventory.filled.md` | no | Generated, id-filled inventory. |
| `coins/rebuild_dedup_state.json` | **yes** | Live coin pack set names/order. |
| `coins/rebuild_dedup_plan.json` | no | Frozen coin upload plan. |
| `coins/remap_live_cache.json` | no | remap signature cache. |
| `coins/ticker_to_id.prebroken.json` | no | Backup of the pre-fix map. |
| `logs/*.log` | no | Per-run UTC logs. |
| `state_<base>.json` | no | `build_pack` resume state. |
| `assets/vendor/lottie_svg.min.js` | **yes** | Vendored Lottie player (offline). |

---

## 26. Security & secrets

- Tokens and the owner id live only in `.env` (and a local `secrets.md`
  registry). Both are gitignored and must never be committed, printed, logged,
  or pushed.
- `emojikit.logsetup` raises `urllib3`/`requests` to WARNING so the Telegram API
  URL (which contains the token in the path) is never written to a log file, and
  `redact()` masks token-shaped strings in any message it does log.
- A leaked bot token must be revoked via @BotFather (`/revoke`) and replaced in
  `.env`.
- The repository is **private** and **All Rights Reserved** (`LICENSE`).

---

## 27. History of real bugs fixed (and the guards that prevent them)

These were found with real data; the guards must not regress.

1. **Scrambled ticker→id map** — position-based mapping shifted by skipped
   uploads. Fix: content-based `remap_ids.py`; builds now record actual upload
   order. (§17)
2. **Wrong source logo (e.g. Solana)** — a ticker collision pulled a different
   token's logo. Fix: `verify_logos --fix --only`, plus replacing the source
   asset. (§7.2)
3. **64-bit phash overflow** — unsigned dHash exceeded SQLite's signed range and
   dropped rows. Fix: signed storage + restore. (§13.4)
4. **Over-aggressive dedup** — perceptual merge (threshold 5) collapsed 80
   distinct emoji to 70. Fix: near-dup merging **off by default**. (§16.2)
5. **Blank emoji from gradient SVG** — svglib renders some gradients blank. Fix:
   blank detection + raster fallback; never upload blank. (§15.1)
6. **reportlab 3.12 wheel** — `reportlab<4` has no cp312 wheel. Fix: target
   Python 3.11 in runtime + CI. (§3)
7. **Token in logs** — urllib3 DEBUG logged the bot-token URL. Fix: silence those
   loggers + `redact()`. (§26)
8. **Panel image 404** — `/img/<key>` wasn't URL-decoded. Fix: `unquote`. (§20)
9. **Resume duplicate** — positional resume could re-upload after a skip. Fix:
   dedup by the per-item committed `uploaded` flag + persisted `skipped`. (§6.4)

---

## 28. Keeping this guide in sync

When you add or change any command, flag, file, table, or workflow:

1. Update the matching section here **in full** (0 → 100, with an example).
2. Update `README.md` if the change is user-facing.
3. Run the unit tests + a real run; verify UI in a browser when relevant.
4. Commit and **push to `main`** so GitHub stays in sync.

*End of guide.*
---

## Appendix A — Command cheat-sheet

All commands run from the project root. `PY = .venv\Scripts\python.exe` (Windows)
or `.venv/bin/python` (macOS/Linux).

```powershell
# --- setup ---
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # then edit tokens + owner id
.\run.ps1                         # launcher menu
.\run.ps1 -Check                  # env doctor (CI-style)

# --- single pack (general) ---
$PY make_emoji_pngs.py --in input\set --out build\set
$PY build_pack.py --base set --title "Set" --source-dir build\set --token-env GENERAL_BOT_TOKEN --dry-run
$PY build_pack.py --base set --title "Set" --source-dir build\set --token-env GENERAL_BOT_TOKEN

# --- collector ---
$PY fetch_pack.py <pack-or-link> --token-env GENERAL_BOT_TOKEN
$PY fetch_emoji_ids.py --ids-file <file-with-premium-ids> [--id <n>]
$PY add_media.py --in input\set --emoji 😀
$PY panel.py                                            # curate, then Save
$PY build_collection.py --base mypack --title "My Pack" --token-env GENERAL_BOT_TOKEN --dry-run
$PY build_collection.py --base mypack --title "My Pack" --token-env GENERAL_BOT_TOKEN

# --- bot ---
$PY emoji_bot.py

# --- coins ---
$PY coins\fetch_logos.py
$PY make_emoji_pngs.py --in coins\logos\svg --out coins\logos\emoji
$PY make_emoji_pngs.py --in coins\logos\png --out coins\logos\emoji
$PY coins\rebuild_dedup.py
$PY coins\remap_ids.py --emoji-dir "F:\...\emoji" --max-distance 200 --apply
$PY coins\enhance_map.py
$PY coins\alias_map.py
$PY coins\check_all_packs.py
$PY coins\verify_logos.py --emoji-dir "F:\...\emoji"
$PY coins\verify_logos.py --emoji-dir "F:\...\emoji" --fix --only sol,xrp
$PY coins\write_manifests.py --out-dir "F:\...\@GodVerify Crypto Emoji"

# --- tests / CI-locally ---
$PY -m unittest discover -s tests -p "test_*.py"
$PY -m compileall -q .
$PY -c "import build_pack, make_emoji_pngs, fetch_pack, fetch_emoji_ids, add_media, build_collection, emoji_bot, panel"

# --- git ---
git add -A; git commit -m "..."; git push origin main
gh run list --repo KiaroSama/Emoji-Mapper --limit 1 --json status,conclusion
```

---

## Appendix B — Example session transcripts (illustrative)

### B.1 Fetch + curate + publish

```
> python fetch_pack.py https://t.me/addemoji/RMaccs --token-env GENERAL_BOT_TOKEN
[..] Authenticated bot: @GodVerifyEmojiMapperbot
[..] Pack RMaccs (Accounts store — @RMaccs): 80 stickers -> new=80 dedup=0 failed=0
Done. new=80 dedup=0 failed=0
  catalog static: 80 total (80 pending upload)

> python panel.py
Emoji curate panel: http://127.0.0.1:8765/
# (open browser, untick a few, click Save -> "Saved ✓  76 included · 4 excluded")

> python build_collection.py --base accts --title "Accounts" --token-env GENERAL_BOT_TOKEN --dry-run
DRY RUN: nothing uploaded.
  static: 76 emoji -> 1 set(s) named acctss1_by_<bot> ...
  video: 0 emoji -> 0 set(s) ...
  animated: 0 emoji -> 0 set(s) ...
```

### B.2 Resolve an id and download its pack

```
> python -c "...getCustomEmojiStickers(['5283254221590787816'])..."
set_name: RMaccs | format: static
> python fetch_pack.py RMaccs --token-env GENERAL_BOT_TOKEN
```

### B.3 Audit the coin packs

```
> python coins\check_all_packs.py
...
total stickers audited: 5791
BLANK stickers: 0
DUPLICATE image groups: 0 (extra duplicate stickers: 0)
```

> Transcripts are illustrative; exact counts depend on your data and Telegram
> rate limits at the time.

---

## Appendix C — Conventions for contributors / agents

- **Language:** all code, comments, filenames, logs, docs in English.
- **Placement:** source at root or in `emojikit/`/`coins/`; tests in `tests/`;
  docs in `docs/`; vendored assets in `assets/vendor/`. Don't clutter the root.
- **No new dedup/mapping mechanisms** — extend the catalog (§5, §13, §16).
- **Never** commit `.env`, `secrets.md`, `collection/`, `logs/`, tokens.
- **Logging** is mandatory for executable scripts via `emojikit.logsetup`
  (UTC file logs; secrets redacted).
- **Verify before claiming done:** unit tests + a real run; for UI, a real
  browser (Playwright) at the relevant breakpoints.
- **Sync:** push to `main` and keep this guide + `README.md` current.

*This guide is the single source of truth for how Emoji Mapper works. If code
and guide disagree, fix whichever is wrong and re-sync.*
