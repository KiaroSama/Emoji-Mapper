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
| **Crypto coins** (`coins/`) | `TELEGRAM_BOT_TOKEN` (`@YourCoinEmojiBot`) | the original coin-logo packs (29 packs, ~5.8k emoji) |
| **General / collector** | `GENERAL_BOT_TOKEN` (`@YourEmojiBot`) | build any pack, copy packs, extract IDs |

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
GENERAL_BOT_USERNAME=YourEmojiBot
GENERAL_BOT_NAME=YourBrand Emoji Mapper
CMC_API_KEY=<optional CoinMarketCap key, only for coins/fetch_cmc.py>
```

External tool: **ffmpeg + ffprobe** on `PATH` are required **only** for video
emoji. Install on Windows: `winget install Gyan.FFmpeg`.

Pinned deps note: `svglib<2` / `reportlab<4` are intentional — they ship the
bundled SVG rasterizer (no system cairo). These wheels exist for Python 3.11,
not 3.12, so the project targets 3.11 and CI runs on 3.11.

---

## 4. The launcher (`run.ps1`)

Right-click → *Run with PowerShell*, or `.\run.ps1`. It bootstraps `.venv`,
checks deps + ffmpeg, then shows a menu:

```
1) Build a general emoji pack (new bot)
2) Convert images to 100x100 PNGs only
3) Crypto-coin pack rebuild (coin bot)
4) Collect emoji from existing packs (download)
5) Add media from a folder (build from scratch)
6) Publish the collection into new packs
7) Run the Emoji Mapper bot (premium-emoji ID extractor)
8) Curate panel — pick which emoji to include (web)
q) Quit
```

Non-interactive health check (used by CI / scripts): `.\run.ps1 -Check`.
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
`<base>a<n>` (animated) — Telegram cannot mix formats in one set. Each finished
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
.venv\Scripts\python.exe emoji_bot.py    # uses GENERAL_BOT_TOKEN; or run.ps1 -> 7
```

- Send it a **premium emoji** → replies with the ID on a tap-to-copy button.
- Send/forward a **post with premium emoji** → lists every ID; tap to copy.
- **Add it to a channel/group** (as admin) → DMs the owner the premium-emoji IDs
  from *new* posts (Bot API cannot read past channel history).
- `/start`, `/help` show the menu (registered via `setMyCommands`).

Tap-to-copy uses Telegram's `copy_text` inline button (Bot API 9.0).

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
