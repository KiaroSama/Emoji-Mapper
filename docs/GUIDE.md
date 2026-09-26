# Numera Emoji Mapper — Complete Guide (0 → 100)

This is the full reference for **Numera Emoji Mapper**. It is written so that any
person **or AI agent** can read it once and understand how the project works and
how to perform every workflow and the next steps correctly. Keep this file in
sync with the code: whenever a command, flag, file, or workflow changes, update
the matching section here.

> Free software under the GNU GPL v3 or later (see [`LICENSE`](../LICENSE)).
> Telegram bot tokens and the owner id live only in `.env` / `secrets.md`
> (never committed). Never print or commit secrets.

---

## 1. What this project does

Numera Emoji Mapper turns images/animations into **Telegram premium custom-emoji
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
Numera Emoji Mapper/
  run.ps1                  Windows launcher (menu). Prefer this.
  emojikit/                command modules and shared toolkit
    build_pack.py            core engine: upload a folder of media to emoji sets
    make_emoji_pngs.py       image -> 100x100 PNG (static)
    fetch_pack.py            collector: download a Telegram pack -> catalog
    fetch_emoji_ids.py       collector: download specific emoji by ID -> catalog
    add_media.py             collector: build emoji from local files -> catalog
    build_collection.py      collector: publish the catalog into new packs
    sync_order.py            reorder an already published pack (no re-upload)
    pack_archive.py             # archived media reconciliation
    pack_manifest.py            # pack roster and gallery CLI
    panel.py                 web "Curate" panel: the server, the page, the APIs
    emoji_bot.py             interactive bot: extract premium-emoji IDs (tap-to-copy)
    telegram_api.py           the Bot API client, its errors and Telegram's caps
    packstate.py              state-file shape + atomic write + pack-family lock
    announce.py               announce finished packs (Worker, or direct)
    collection_state.py       publisher plan/resume state + brand logo
    operator_config.py        the operator's identities from .env; unset = stop, no default
    collection_reconcile.py   what is live in a set, and whose key each sticker is
    collection_migrate.py     a content-key migration as ONE versioned change
    collection_preflight.py   --preflight only: ask Telegram to validate the queue
    panel_view.py             the same panel's view model (build_view, ordering)
    pack_gallery.py           self-contained pack gallery rendering
    ingest.py                 verified dedup and collision-safe media storage
    panel_preview.py          bounded, sized thumbnail cache
    panel_logging.py          validated, bounded browser event logs
    logsetup.py            UTC file logging (logs/)
    media.py               format detect + conversions (static/video/tgs)
    video_decode.py        the video decoder choice + a bounded frame cache
    errors.py              shared exception types (no import cycle)
    repaint.py             baking Telegram's tint into a Lottie or a static
    identity.py            content keys, perceptual hashes, same_image
    catalog.py             content-addressed SQLite catalog (dedup + inclusion)
  coins/                   the crypto-coin component (see §7)
    _paprika_api.py        CoinPaprika HTTP + candidate search + logo decode
  scripts/check.ps1        byte-compile + full unit suite (CI runs this too)
  scripts/identity_repair.py  report/migrate catalog keys after a decode fix
  tests/                   unit tests + fixtures (see tests/README.md and §10)
  docs/GUIDE.md            this file
  .env.example             config template
  requirements-dev.txt     test-only deps (playwright, for the browser suite)
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
# Core manifest: requests + Pillow + resvg-py + rlottie-python. Everything
# except one coin tool
# runs on this alone.
.venv\Scripts\python.exe -m pip install -r requirements.txt
# Coin extra: numpy, imported only by coins/remap_ids.py (~20 MB wheel + BLAS,
# so it is not in the core set). Skip it unless you work on coins/.
.venv\Scripts\python.exe -m pip install -r requirements-coins.txt
# Test extra: ruff + playwright, for scripts\check.ps1 and the panel's browser
# suite. Nothing in the runtime imports either. Skip it unless you run tests.
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m playwright install chromium

# 2. Configure secrets
copy .env.example .env
# edit .env and fill in tokens + owner id (see keys below)
```

`.env` keys:

```
PACK_OWNER_USER_ID=<your numeric Telegram id>   # owns every created set; press Start on each bot once
TELEGRAM_BOT_TOKEN=<coin bot token>
GENERAL_BOT_TOKEN=<general bot token>
BOT_ALLOWED_USER_IDS=<optional: extra ids allowed to use emojikit/emoji_bot.py>
PACK_LINKS_CHAT_ID=<optional: channel that receives finished-pack links>
WORKER_PUBLISH_URL=<optional: https://<worker>.workers.dev/publish — see §12.9>
WORKER_PUBLISH_SECRET=<optional: bearer for that endpoint; both or neither>
EMOJI_LOG_RETENTION_DAYS=30    # optional: prune logs/ older than N days (0 = keep all)
EMOJI_FFMPEG_TIMEOUT=300       # optional: seconds per ffmpeg/ffprobe child
CMC_API_KEY=<optional CoinMarketCap key, only for coins/fetch_cmc.py>

# Your identities -- required where used, NO default (the repo names no operator)
BRAND_LOGO_BOTS=<bots whose sets lead with your logo; EMPTY = none, unset = stop>
BRAND_LOGO_PATH=private/brand-logo.png   # your logo; private/ is git-ignored
BRAND_LOGO_KEYWORDS=<optional: logo emoji keywords, default logo>
COLLECTION_PACK_BASE=<your general collection's --base>
COIN_PACK_BASE=<your coin set-name base>
COIN_PACK_TITLE=<your coin set title>
EMOJI_ARCHIVE_DIR=<where full packs are archived>
COIN_EMOJI_DIR=<where the coin logo PNGs live>
```

That is the complete set of variables any code reads, plus two environment-only
switches used for testing: `TELEGRAM_API_BASE` (Bot API endpoint override,
default `https://api.telegram.org`) and `NUMERA_EMOJI_MAPPER_NO_DOTENV=1` (makes
`emojikit.build_pack.load_env()` a no-op; it is read *before* `.env`, so it only works
from the real environment — the test suite sets it there). There is no
`GENERAL_BOT_USERNAME` / `GENERAL_BOT_NAME`: every tool takes the bot's username
from `getMe` at runtime, so a stale copy can never name the wrong bot in a set
name.

External tool: **ffmpeg + ffprobe** on `PATH` are required **only** for video
emoji. Install on Windows: `winget install Gyan.FFmpeg`.

SVG note: SVG rasterizing uses `resvg-py`, a self-contained Rust renderer
shipped as a prebuilt wheel — no system cairo and no build toolchain. It renders
straight to RGBA (gradients included). 3.11 remains the reference runtime; CI
also exercises 3.12.

---

## 4. The launcher (`run.ps1`)

Right-click → *Run with PowerShell*, or `.\run.ps1`. It shows a centered banner
(pink title + full-width rule + yellow `Logging to: ...`), quietly prepares
`.venv` (env/Python/ffmpeg OK lines go to the **log only**, keeping the console
clean), then shows a **colored, sectioned** menu. Sections are lettered in order
(**A** = Build, **B** = Collection, **C** = Bot, **D** = Maintenance) and each
has its own numbering, so keys stay unique — type e.g. `A1`, `B3`, `D1`:

```
                              Numera Emoji Mapper
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
  B4) Open web panel to pick & reorder emoji (browser)

Bot
  C1) Run the Numera Emoji Mapper bot (premium-emoji ID extractor)

Maintenance
  D1) Run the project checks (byte-compile + unit tests)

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
- **No double-upload on network failures**: `addStickerToSet` /
  `createNewStickerSet` are NOT idempotent, so a timeout after Telegram already
  applied the call is never blindly re-sent. `Telegram._call` verifies the live
  set first (applied → success; not applied → safe retry; unknown →
  `AmbiguousUploadError`), and `build_collection` reconciles every unrecorded
  live sticker back to its catalog item (by `file_unique_id`, else by
  downloaded content) before computing what is still pending. Upload payloads
  are sent as bytes so a retried request re-sends the full file.
- **Published copies are remembered**: after publishing, each uploaded copy's
  `file_unique_id` is recorded in `seen_files`, so fetching your own published
  packs (or ids inside them) never downloads anything again.
- **Idempotent / resumable**: re-running fetch or publish is safe and cheap.
- **Curation**: each row has an `included` flag (default 1). The Curate panel
  toggles it; `build_collection` only publishes `included` rows.

`emojikit/media.py` cheat-sheet: `detect_format`, `to_static_png`,
`to_video_webm` (ffmpeg, VP9, ≤256 KB), `to_animated_tgs` (Lottie→gzip),
`validate_video`, `validate_tgs`, `reencode_in_place`.

`perceptual_hash` premultiplies by alpha before reducing to grey, because
`convert("L")` on RGBA discards alpha and the RGB under a transparent pixel
is whatever the last encoder left there.

`emojikit/identity.py` cheat-sheet: `content_key` (the catalog's primary
key), `perceptual_hash`, `fingerprint` (both in one pass), `same_image`
(did OUR file produce THAT sticker? `None` when undecidable), `hamming`.
It imports `media`; `media` never imports it.

---

## 6. Collector workflows (0 → 100)

### 6.1 Download an existing pack into the catalog

```powershell
# By pack short-name or t.me/addemoji/<name> link. Use the bot that can read it.
.venv\Scripts\python.exe -m emojikit.fetch_pack <pack_or_link> [<pack2> ...] `
    --token-env GENERAL_BOT_TOKEN [--data-dir collection] [--limit N] [--phash-threshold -1]
```

Find which pack an emoji ID belongs to first (then fetch that pack):

```powershell
# getCustomEmojiStickers returns set_name + is_animated/is_video
# (see emojikit.emoji_bot.enrich_labels for the call; or a one-off snippet)
```

### 6.2 Build emoji from your own files

```powershell
.venv\Scripts\python.exe -m emojikit.add_media --in input\myset --emoji 😀 [--as auto|static|video|animated]
# auto: still image -> static; animated GIF/MP4/WEBM -> video; Lottie .json/.tgs -> animated
# (animated emoji are VECTOR only; a GIF/video becomes a VIDEO emoji, not animated)
```

### 6.3 Curate — pick what to publish (web panel)

```powershell
.venv\Scripts\python.exe -m emojikit.panel [--data-dir collection] [--port 9450] [--preview-fps 15] [--no-open]
                                   [--with-pack N ...] [--all]
```

**An emoji already live in a pack is HIDDEN by default**, so a bare `emojikit/panel.py`
shows the next pack's candidates and nothing else. `--with-pack N` un-hides one
published set so it can be rearranged beside them; repeat the flag per pack.
`--all` brings back every finished pack, which is usually hundreds of cards you
cannot act on. `run.ps1 -> B4` reads the published sets from the publisher's own
state file and offers them (`[Y/n]`, Enter = yes) rather than leaving the grid
looking empty.

Dark neon panel: every emoji is a big labelled card (static=image,
video=`<video>`, animated `.tgs`=pre-rendered to animated WebP). All selected by
default. Click to toggle, **Shift+click** for a range. Look-alikes are ordered
adjacently. **Zoom** with `−` / `+`, Ctrl+wheel, or type an exact percentage
into the field between them (Enter applies it, double-click resets to 100%);
the grid is virtual, so a thousand cards cost what a hundred do. Click
**Save** → writes the `included` flag to the catalog.

**Selection mode** (the pill next to Zoom) is for moving several emoji as one
group. In the holding tray, click a card anywhere to pick it and Shift-click
another to take the run between them; in the grid, drag across the small pick
box in a card's top-left corner to select a
run, then drag any picked card to carry the whole set. While it is on, Select
all / Deselect all / Invert act on the picks instead of on publish inclusion.
**Holding** (the strip under the count line) is a place to park emoji out of
the way: drag one onto it to exclude it from the next publish without hunting
for its tick in a long grid, drag a parked one back in to re-include it at
the exact position dropped — a pack short a few emoji still auto-fills from
whatever candidates follow it, same as unticking one in place always has.

### 6.4 Publish the catalog into new packs

```powershell
# Preview (no upload):
.venv\Scripts\python.exe -m emojikit.build_collection --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN --dry-run
# Publish for real (resumable, duplicate-proof, per-format sets):
.venv\Scripts\python.exe -m emojikit.build_collection --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN [--formats static,video,animated] [--per-set 200]
```

Sets are named `<base>s<n>_by_<bot>` (static), `<base>v<n>` (video),
`<base>a<n>` (animated) — split by format for organization (since Bot API 7.2 a
set *may* mix formats, so this is a choice, not a requirement). Each finished
pack DMs the owner its `t.me/addemoji/...` link, and a per-pack manifest
(`collection/manifests/<set>.md`: name + emoji ID) is written. The manifest
counts the **pack**, not the catalog rows: when a brand logo leads the set it is
row 1 and the catalog items follow from 2, because the logo is a sticker in the
pack even though it is not a catalog item.

---

## 7. Crypto-coin component (`coins/`)

Self-contained tool that reuses `emojikit/build_pack.py` and the coin bot.

```powershell
# Logos (data): fetch + keywords
.venv\Scripts\python.exe coins\fetch_logos.py          # CoinGecko logos + keywords.csv
.venv\Scripts\python.exe coins\fetch_paprika.py --dry  # resolve + report only, nothing published
.venv\Scripts\python.exe coins\fetch_paprika.py        # fill from CoinPaprika
.venv\Scripts\python.exe coins\fetch_cmc.py            # fill from CoinMarketCap (needs CMC_API_KEY)
.venv\Scripts\python.exe coins\build_keywords.py       # (re)build keywords.csv from logos

# Convert logos to 100x100 emoji PNGs
.venv\Scripts\python.exe -m emojikit.make_emoji_pngs --in coins\logos\svg --out coins\logos\emoji
.venv\Scripts\python.exe -m emojikit.make_emoji_pngs --in coins\logos\png --out coins\logos\emoji

# Build / rebuild the packs (duplicate-proof, records upload order)
.venv\Scripts\python.exe coins\rebuild_dedup.py        # build + map + send links
.venv\Scripts\python.exe coins\rebuild_dedup.py map    # only rebuild the id map + inventory
.venv\Scripts\python.exe coins\rebuild_dedup.py links  # resend the combined links message
```

### 7.1 The ticker → custom_emoji_id map (`coins/ticker_to_id.json`)

This is the lookup table consumers use. **Always derive it from image content,
not from positions** (a historical position-based bug scrambled it):

```powershell
# 1) Calibrate: run WITHOUT --apply and pick a cutoff from the reported distances.
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "PATH\to\emoji"
# 2) Apply. --max-distance is REQUIRED with --apply (must be > 0): an uncalibrated
#    run would accept a nearest-but-wrong match and overwrite the map with it.
#    --min-margin (default: --max-distance) additionally rejects a match whose
#    runner-up is nearly as close. A refused --apply writes ticker_to_id.candidate.json
#    instead, for review.
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "PATH\to\emoji" --max-distance 200 --apply

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

## 8. The Numera Emoji Mapper bot (`emojikit/emoji_bot.py`)

Long-polling bot (run it and leave it running; only one instance at a time):

```powershell
.venv\Scripts\python.exe -m emojikit.emoji_bot    # uses GENERAL_BOT_TOKEN; or run.ps1 -> C1
```

- Send it **one or more premium emoji in a row** (spaces/newlines between them
  don't matter) → it replies with a **single collapsed (expandable) quote** of
  `emoji + ID` (each line shows the **actual premium emoji** via a `<tg-emoji>`
  custom-emoji entity next to `<code>id</code>`; tap an ID to copy just that one
  on mobile), plus one or more **“Copy”/“Copy a-b” inline buttons** (`copy_text`)
  covering that message's IDs (each button is capped at 256 chars, ~12 IDs, by
  Telegram itself). As many IDs as fit in one message (~4096-char Telegram limit)
  are batched together, so a 50-id result is ~2 messages, not one-message-per-12.
  (The quote is collapsed by height, so long lists show a few lines until
  expanded — expected, not missing data.)
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
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt    # once: ruff + playwright, not runtime deps
.venv\Scripts\python.exe -m playwright install chromium            # once: the panel's browser suite
.\scripts\check.ps1                                                # compile + lint + full suite
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"   # the suite alone
.\run.ps1 -Check                                                   # env doctor (venv/deps/ffmpeg/.env)
```

**The panel's browser suite needs a browser.** `tests/test_panel_browser.py`
drives the real page in headless Chromium, because that is the only level at
which the client-side behaviour exists — a save pipeline that drops the newest
edit is perfectly well-formed JavaScript, and a source-text assertion cannot
tell it from a correct one. A missing playwright or Chromium is a **hard error,
never a skip**: a browser test that reports green on a machine with no browser
is worse than no browser test at all. Set `NUMERA_EMOJI_MAPPER_NO_BROWSER_TESTS=1` to
opt out deliberately, and know that you did.

`scripts\check.ps1` is the single command CI and a developer both run, so the two
cannot drift into different invocations. Three stages, in order: `compileall`,
`ruff check .`, then the suite — the cheap gates first, so a syntax or lint
error fails in seconds instead of after ~75 s of tests. It resolves the repo
root from its own location (any cwd, spaces in the path are fine), prefers the
repo `.venv` for both Python and ruff, bounds each step with a wall-clock
ceiling (`-TimeoutSeconds`, default 1800; a step that exceeds it is killed and
reported as exit 124), and returns a real exit code. `-Python <path>` overrides
the interpreter. It is also `run.ps1` menu entry **D1**.

**Lint.** `ruff check .` takes no arguments on purpose: `ruff.toml` at the repo
root owns the rule set and the exclusions, so a `--select` on one command line
is all it takes for CI and a local run to start linting different things. The
set is deliberately narrow — ruff's default rules plus `E402`, `BLE001`, `B` and
`RUF100`. The reason is historical: the source already carried ~120
`# noqa: E402` / `# noqa: BLE001` comments written against a linter that was
never configured, so they suppressed nothing and were never checked. Enabling
exactly the codes they name is what makes them meaningful, and `RUF100` (unused
`noqa`) is what stops them rotting back into decoration. Never make the stage
green with `--exit-zero` or `continue-on-error`.

**`-t .` is required, not cosmetic.** Without it the tests directory becomes the
top level, modules load as `test_x` instead of `tests.test_x`, and
`tests/__init__.py` — which scrubs every credential-shaped variable out of the
environment and refuses non-loopback sockets — never runs.
`tests.test_entry_points.SuiteIsHermetic` fails loudly when the suite is started
without it. Details in [`tests/README.md`](../tests/README.md).

**The Worker suite runs in CI, in its own job.** `worker/` is TypeScript with
its own vitest suite. It is a separate `worker:` job rather than a step in
`build:` because it shares nothing with the Python matrix — it needs Node, not
Python and ffmpeg — and running it once per Python version would be pure waste.
A separate job also makes a Worker failure legible as a Worker failure.

**So does the panel's browser suite**, in `panel-browser:`, for the same reason:
it exercises JavaScript, so running it in the matrix would download Chromium
once per Python version to prove the same thing. The matrix sets
`NUMERA_EMOJI_MAPPER_NO_BROWSER_TESTS=1` explicitly — an opt-out that is written down
is the only kind this module accepts.

`check.ps1` stays Python-only: it is the command a developer runs constantly,
and requiring a Node toolchain for it would tax everyone who never touches
`worker/`. Run the Worker's own checks after changing it:

```bash
cd worker && npm ci && npm run typecheck && npx vitest run
```

CI uses `npm ci`, never `npm install`: it installs exactly the committed
lockfile and fails if `package.json` and the lock disagree, so CI cannot quietly
test a different dependency tree than the one committed. `dependabot.yml` has an
`npm` entry for `/worker` so that tree gets updates like every other one.

CI (`.github/workflows/ci.yml`, Python 3.11 **and** 3.12): installs both
dependency manifests + ruff + ffmpeg, runs `ruff check .`, the import smoke
test, then `scripts/check.ps1` (compile + lint + suite, `shell: pwsh`), then an
offline `build_pack` dry-run. Run `scripts\check.ps1` locally before pushing.

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
   order; never reintroduce position-offset resume logic. Never blind-retry a
   non-idempotent Bot API call: go through `Telegram.add_emoji`/`add_sticker`
   with `expected_before=<live count>` (verified retry) and let
   `emojikit.build_collection.reconcile_set` attribute anything ambiguous from the live
   set before uploading more.
5. **Curation**: respect the `included` flag in any new publish path.
6. **UI changes** (panel): keep the dark neon-blue style, Inter font, visible
   focus, `prefers-reduced-motion`, lazy media (IntersectionObserver) so large
   catalogs stay fast. Verify in a real browser before claiming done.
7. **External libs/APIs**: check current docs before coding (the Telegram Bot
   API and any JS player evolve).
8. **Always**: add/maintain tests, run `.\scripts\check.ps1` (never a hand-rolled
   `unittest` invocation — see §10) plus a real run, then commit and **push to
   keep GitHub in sync**, and **update this guide** with any new command 0 → 100.

---

*Keep this document synchronized with the code. When you add or change a
command, add it here in full.*
---

## 12. Full CLI reference

Every entry point, every flag, with defaults and examples. All commands assume
you run them from the project root with the venv Python
(`.venv\Scripts\python.exe`). On macOS/Linux use `.venv/bin/python`.

### 12.1 `emojikit/make_emoji_pngs.py` — image → 100×100 PNG

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
- **`logos/.incoming/<run>/`** is staging, not a source folder. Each fetcher run
  gets its OWN subdirectory: the download happens before any lock is taken, so a
  path shared by ticker name could be replaced by a second fetcher between this
  run's hash, its upload, its verification and its promotion. A logo moves into
  `logos/emoji/` only once its upload is confirmed and the map records its id,
  so `logos/emoji/<ticker>.png` always describes a sticker that really exists —
  the other tools use that file to decide which live sticker belongs to which
  ticker. A run deletes only its own staging directory, never another's, which
  may still be mid-upload; a directory left by a killed run is harmless and can
  be removed by hand once no fetcher is running.

  One file survives on purpose: if a run ends with an **unresolved** upload (the
  add reached Telegram but could not be confirmed), the image it sent is kept so
  the next run can prove what landed. Deleting it would leave that run falling
  back to its own fresh download of the same ticker and publishing *that* as the
  logo for a sticker made from the original image. It is removed once the
  unresolved upload is settled. If it is ever lost anyway, the next run still
  identifies the live sticker and updates the map, but says plainly that
  `logos/emoji/<ticker>.png` was **not** updated — re-fetch the ticker to refresh
  it.

Examples:

```powershell
.venv\Scripts\python.exe -m emojikit.make_emoji_pngs --in input\myset --out build\myset
.venv\Scripts\python.exe -m emojikit.make_emoji_pngs --in input\myset --limit 50
.venv\Scripts\python.exe -m emojikit.make_emoji_pngs            # legacy coin mode
```

### 12.2 `emojikit/build_pack.py` — upload a folder of PNGs to emoji sets

| Flag | Default | Meaning |
|------|---------|---------|
| `--base` | *(required)* | Set-name base (letters/digits/`_`). |
| `--title` | *(required)* | Human-readable set title. |
| `--source-dir` | `logos/emoji` | Folder of 100×100 PNGs to upload. |
| `--token-env` | `TELEGRAM_BOT_TOKEN` | Env var holding the bot token. |
| `--keywords` | `auto` | keywords CSV; `auto` = coin `keywords.csv` only for the default source. |
| `--emoji` | `🪙` | Associated standard emoji. |
| `--user-id` | `PACK_OWNER_USER_ID` | Numeric owner id. |
| `--per-set` | `200` | Emojis per set. Accepted range is **1–200** (Telegram's cap); anything larger exits 2 with a usage error. |
| `--limit` / `--start` | `0` / `0` | Process a slice of the source. |
| `--state` | `state_<base>.json` | Resume file (per pack, never clobbered). |
| `--dry-run` | off | Validate inputs without calling Telegram. |

Resumable: progress is saved to `state_<base>.json`; an interrupted/flood-limited
run continues without recreating existing sets. Use `--dry-run` first.

```powershell
.venv\Scripts\python.exe -m emojikit.build_pack --base myset --title "My Emojis" `
    --source-dir build\myset --token-env GENERAL_BOT_TOKEN --emoji 😀 --dry-run
```

### 12.3 `emojikit/fetch_pack.py` — download a Telegram pack into the catalog

| Flag | Default | Meaning |
|------|---------|---------|
| `packs...` | *(required)* | One or more pack short-names or `t.me/addemoji/<name>` links. |
| `--token-env` | `GENERAL_BOT_TOKEN` | Bot token env var. |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--phash-threshold` | `-1` (off) | Hamming distance for near-dup merging; `-1` keeps look-alikes. |
| `--limit <n>` | `0` (all) | Max new items per pack. |
| `--repaintable` | `ask` | What to do with emoji Telegram REPAINTS (`ask`/`skip`/`keep`). The client overrides their colours, so the source pack does not show the stored art: in one of ours they render that art instead — sometimes flat black, sometimes full colour. Look before you decide; with no terminal to answer, `ask` skips them. |

Re-running is cheap: stickers whose `file_unique_id` was already ingested are
skipped without downloading; identical media collapse to one catalog row.

### 12.3b `emojikit/fetch_emoji_ids.py` — download *specific* emoji by ID (not whole packs)

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
| `--repaintable` | `ask` | What to do with emoji Telegram REPAINTS (`ask`/`skip`/`keep`). The client overrides their colours, so the source pack does not show the stored art: in one of ours they render that art instead — sometimes flat black, sometimes full colour. Look before you decide; with no terminal to answer, `ask` skips them. |
| `--tint <#RRGGBB>` | *(off)* | Bake the repaint into the asset: flatten every REPAINTABLE emoji to this colour, keeping its silhouette — the same thing a client does, done by us because the flag itself cannot be set on an existing set. Answers `--repaintable`, so nothing is skipped. Animated goes through the Lottie so the animation survives; static fills through the alpha; video is refused. Recorded on the item as `tint:#RRGGBB`. |

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
.venv\Scripts\python.exe -m emojikit.fetch_emoji_ids `
  --ids-file "...\GV Swap bot\bot-emoji-inventory-user.md" `
  --ids-file "...\GV Swap bot\bot-emoji-inventory-admin.md" `
  --ids-file "...\YourBrand Payment Bot\bot-emoji-inventory-admin.md" `
  --ids-file "...\YourBrand Payment Bot\bot-emoji-inventory-user.md"
# -> collected 240 real id occurrences -> 101 unique (83 ids duplicated across files)
# -> Done. unique_ids=101 new=100 dedup=1 failed=0 missing=0
```

### 12.4 `emojikit/add_media.py` — build emoji from local files into the catalog

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

### 12.5 `emojikit/build_collection.py` — publish the catalog into new packs

**`--mixed` publishes every format into ONE family**, named `<base><n>_by_<bot>`
with no format letter, in the curate panel's order. Without it each format gets
its own sets — which was this tool's default from before Bot API 7.2 allowed
mixed sets, and it costs the curation: the panel's order runs *across* formats,
so splitting regroups a hand-arranged pack into format blocks. It is a flag
rather than the new default for one concrete reason: `state["sets"]` and the
frozen plan are keyed by format, so flipping it would make an existing
half-published family unresumable. A family started one way refuses to continue
the other, with a usage error rather than a stranded set.

`--base` follows Telegram's own rule for a set name: letters, digits and
**single** underscores, beginning with a letter. The `_by_<bot_username>` tail
is appended and is **not optional** — Telegram rejects a name without it. The
64-character limit is checked against the real bot username before the first
upload, not discovered as a Bot API error after the plan is frozen.


**Set titles are one sequence across every format.** `--title "@YourBrand Emoji
Packs"` produces `@YourBrand Emoji Packs 1`, `2`, `3` … in creation order,
whatever format each set holds. They used to carry the format word and count
per format (`… Animated 1`, `… Static 1`), so two different packs were both
called "1". The number counts every set already recorded, which is also what
makes it resumable: a restarted run continues the count instead of restarting
it. Set **names** are unchanged — `<base>s<n>` / `<base>v<n>` / `<base>a<n>`
remain the identity, and only the human title moved.

| Flag | Default | Meaning |
|------|---------|---------|
| `--base` | *(required)* | Set-name base (letters/digits only). |
| `--title` | *(required)* | Human-readable title. |
| `--token-env` | `GENERAL_BOT_TOKEN` | Bot token env var. |
| `--user-id` | `PACK_OWNER_USER_ID` | Numeric owner id. |
| `--emoji` | `😀` | Fallback associated emoji. |
| `--formats` | `static,video,animated` | Which formats to publish, in order. |
| `--per-set` | `200` | Emojis per set. |
| `--new-set` | off | Start this run in a **fresh** set instead of filling the current one. |
| `--into-pack` | *(newest)* | Add to pack **N** instead of the newest one, so any pack with room can be topped up. |
| `--data-dir` | `collection` | Catalog/media directory. |
| `--brand-logo` | `BRAND_LOGO_PATH` | First-emoji brand logo, for the bots in `BRAND_LOGO_BOTS` only. |
| `--no-brand-logo` | off | Disable the mandatory first-emoji logo. |
| `--mixed` | off | Publish every format into ONE family (see above). |
| `--dry-run` | off | Show the plan without uploading. |
| `--preflight` | off | Ask Telegram to validate every queued file, then stop. Publishes nothing. |
| `--repaint` | off | Create NEW sets with `needs_repainting`, so the client paints every emoji in them the text/accent colour. Whole-set and creation-only: it cannot be added later and it flattens colour art. |

**What `--preflight` reports, and what each exit code means.** Four outcomes,
counted apart, because collapsing them is how a run once claimed a validation
it never performed:

| Outcome | Meaning | Effect on the exit code |
|---------|---------|-------------------------|
| accepted | Telegram said the file is uploadable. | — |
| refused | Telegram rejected it (`BotApiError`); the message is printed. | exit 1 |
| missing | The catalog row points at a file that is not on disk. | exit 1 |
| not checked | The request never reached Telegram (transport). | exit 3 |

The last row is the one that matters, and the count of unreachable files does
not change it. A dropped connection is **not** a verdict on the artwork, so it
is never reported as a refusal — exit 1 is what a refusal returns, and
answering a lost connection with it sends someone editing artwork that was
never the problem. It is not reported as an acceptance either: the old counter
counted attempts, so a run in which every single check failed at the transport
printed "all accepted" and exited 0. Exit 3 says the one true thing — the run
did not finish asking. `tests/test_preflight_outcomes.py` pins each of the four.

**Leaving a pack unfinished (`--new-set`).** Normally set *N+1* opens only when
set *N* reaches `--per-set`, so a pack you want to stop early has no way
forward. `--new-set` rolls this run into a fresh set and leaves the current one
at whatever size it has — that is how you start pack 3 while pack 2 is still
half empty. It is consulted **once per run**: later items in the same run fill
the new set normally, so pass it on the run that should open the pack, not on
every run afterwards (each one would open another set). The two workarounds it
replaces are both wrong: a smaller `--per-set` caps every *later* set at the
same wrong size, and a second `--base` starts a new family whose `publications`
table is empty, so the entire catalog would be re-uploaded into it.

**Topping up an older pack (`--into-pack N`).** Publishing always appended to
the newest set, so once pack 3 existed there was no way to put anything back
into a half-empty pack 1 — its remaining room was unreachable. `--into-pack 1`
aims this run at that pack instead.

It fails loudly rather than falling back, because a silent fallback would fill
some *other* pack and still look like success: an unknown number, a full pack,
or one holding a sticker this publisher cannot identify each stop the run. That
last case is not fussiness — appending past an unattributable sticker is what
hands a new key someone else's `custom_emoji_id`.

`--new-set` and `--into-pack` together are rejected: one opens a fresh pack and
the other fills an existing one, so there is no sensible combined meaning.

The subtle part is bookkeeping, not upload. The live count and the recorded key
order used to be written to the *last* set's record, which is the same thing as
the target only while you are filling the newest pack. Filling a middle one
would have credited the upload to the wrong record — state describing a set the
sticker never entered, which is exactly the drift `reconcile_set` exists to
catch. Both now follow the pack actually being written to, and a test pins it.

Only **included** (panel-selected), not-yet-uploaded, non-skipped items are
published. Per-format sets, drift-proof resume, per-pack manifests.

**Brand logo (first emoji of every set).** Your own logo, configured in
`.env` -- the repository ships none, because it names no operator:

| Key | Meaning |
|-----|---------|
| `BRAND_LOGO_BOTS` | Bot usernames whose sets lead with the logo, comma-separated. **Set it empty** for "no bot"; unset stops the publish. |
| `BRAND_LOGO_PATH` | The logo image. Relative paths are under the project root; `private/` is git-ignored for exactly this. |
| `BRAND_LOGO_KEYWORDS` | Optional search keywords for the logo emoji (default `logo`). |

When publishing with a listed bot the logo is inserted as the **first emoji of
every set** (`--brand-logo` overrides the path). A listed bot whose logo file
is missing stops the publish before anything changes: the first slot cannot be
filled afterwards. Since Bot API 7.2 (March 2024) a single custom-emoji set may
contain **mixed formats**, so the logo is always a **static** 100x100 PNG and
leads a static, video *or* animated set alike (verified live). Leave the coin
bot out of `BRAND_LOGO_BOTS` to keep it exempt. Disable with `--no-brand-logo`.
The logo occupies position 0, so item `custom_emoji_id`s are read from position 1 onward (handled automatically).

### 12.5b `emojikit/sync_order.py` — reorder an ALREADY PUBLISHED pack

| Flag | Default | Meaning |
|------|---------|---------|
| `--base <name>` | *(required)* | The published family to reorder. |
| `--data-dir <dir>` | `collection` | Catalog/state directory. |
| `--token-env <VAR>` | `GENERAL_BOT_TOKEN` | Which token owns the packs. |
| `--pack N` | *(all)* | Only reorder pack N (repeatable). Publishing appends, so the live order still has to be applied separately - and applying it family-wide would move stickers in packs you never arranged. |
| `--apply` | off | Actually move stickers. Without it, report only. |

Rearranging the panel after a pack is live does **not** mean republishing it.
`setStickerPositionInSet` moves a sticker that is already in the set, so every
emoji keeps its `file_id` **and** its `custom_emoji_id`: nobody who already
uses one is affected, and no upload happens.

```powershell
$PY -m emojikit.sync_order --base mypack             # what would move
$PY -m emojikit.sync_order --base mypack --apply
```

It is the one set mutation in this project that is genuinely idempotent —
setting the same sticker to the same index twice leaves the same set — so it
may be re-run, and an interrupted run simply continues. Moves are planned as a
selection sort (one API call each, ~120 ms apart), which means a run stopped
halfway leaves a set that is correct up to where it stopped rather than
scrambled.

Two refusals, both about identity:

- **An unrecognised sticker stops that set.** Positions only mean something
  once every sticker is identified; shuffling around an unknown one would move
  a stranger's emoji into the middle of the pack.
- **The brand logo stays at position 0.** It is not a catalog item, so it is
  the one legitimately unknown sticker — and only at index 0.

It takes the same pack-family lock as the publisher: reordering while a publish
appends would move stickers out from under it.

**It also rewrites the order the publisher recorded** (`state["sets"][n]["keys"]`)
to match what it just made live, and does so even when nothing needed moving.
The publisher verifies every recorded position by identity before adding to a
set, so a reorder that left the record behind made the family unpublishable:
`position N now holds a sticker this publisher cannot identify`. A report-only
run writes nothing.

### 12.5c `emojikit/pack_manifest.py` — the roster of what is in every published pack

| Flag | Default | Meaning |
|------|---------|---------|
| `--refresh` | — | Read the live sets and rewrite `packs/`. |
| `--check` | — | Is `packs/` still current? Exit 3 if not. Touches no network. |
| `--family` | `all` | `general` (the 5 packs) or `coins` (the 29 crypto packs). |

Writes three files per set, named after it: `.json` to parse, `.md` to read, and
`.html` to LOOK at — one self-contained page, every thumbnail inlined as a
`data:` URI so animation plays with no player, no CDN and no network, and the
whole roster repeated in a `<script type="application/json">` block so a parser
never has to scrape the markup.

**The page is the curate panel, minus everything that mutates.** Same grid, card,
per-format accent, position pill and checkerboard thumb, and the same view-only
header: ↑ Top, ↓ Bottom, the four-way backdrop cycle and the animation switch.
What is absent is everything that writes — no `draggable`, no tick, no selection,
no save, no form field at all.

**It gates its media like the emojikit.panel.** Every animated card inlines a still as
well as the animation, starts frozen, and only what is on screen is swapped to
the moving version; scrolling freezes everything until 180 ms after it settles,
and `Animation: Off` freezes it permanently. Thumbnails are 88px at 9fps — fps
is the biggest lever on the inlined weight, and a roster is for telling emoji
apart rather than admiring the motion. It is a record; a control that
looks live but saves nothing is worse than none. Each card carries all THREE ids,
labelled and separately click-to-copy: **this pack**, **original pack** where
the emoji came from someone else's, and **this pack, before** — every id the
emoji held in OUR packs earlier, oldest first. Only a replace mints a new
`custom_emoji_id` (`setStickerPositionInSet` and `setStickerEmojiList` leave it
alone), and the retired one is exactly what an external map still points at, so
it is kept rather than overwritten. The catalog cannot supply it: `publications`
is keyed `(base, content_key)`, so a replace overwrites the old id and it is
gone. The roster is therefore its own archive — each `--refresh` reads the
previous roster and carries the trail forward, keyed by `history_key`
(`ck:<content_key>`, `logo:<set>`, or `coin:<tickers>`) so the history survives
both a new id and a move to another pack. `packs/index.json` adds
`by_current_id`, `by_source_id` and `by_previous_id` (a retired id → the id that
took its place) over all 34 packs, plus an `id_changes` count.

Every row gives the emoji's `custom_emoji_id`, its `#` numbered **from 0** (the
brand logo is emoji 0) beside the 1-based `slot` Telegram shows, its format, the
**glyph the sticker carries** (shown under the artwork in this page and in the
curate panel — Telegram never displays it, so these two grids are the only place
the label can be checked against the art), a name, and — when it came from someone else's pack — **the id it had
there**. The coin family carries no brand logo (that bot is exempt), so its
emoji 0 is a real coin and the page says so.

**The order and the ids are read from Telegram, never from the publisher state.**
A live reorder or an in-place replace (which mints a NEW id) would leave a
state-derived roster confidently wrong, and a roster that is quietly wrong is
worse than none.

Thumbnails are cached in `packs/.thumbs/<custom_emoji_id>.<ext>`, keyed on the
id rather than the path because a replaced sticker gets a new id — exactly when
its picture must be re-made. `packs/` is git-ignored: it is derived data, and one
refresh rewrites ~79 MB.

Coin artwork lives outside the repo; point `COIN_EMOJI_DIR` at it, or the coin
pages come out without pictures (the data is unaffected).

**`Pack-Roster-Check` keeps it honest.** A Stop hook runs `--check` and blocks
the turn when the roster no longer matches its inputs — a new download, a
replaced or recoloured sticker, a reorder, a coin remap. It blocks once per
distinct input state, so declining cannot loop, and re-arms on the next change.
Clear it with `--refresh`, and say in the reply that the roster was updated.

### 12.5d `emojikit/pack_archive.py` — the finished pack's media leaves the project

Once a pack is FULL its media moves out to the owner's archive, one folder per
pack, and the project keeps no copy. A pack still being filled is left alone on
purpose: the filename carries the emoji's **slot**, and an unfinished pack can
still be reordered, which would make every name in its folder wrong.

| Flag | Default | Meaning |
|------|---------|---------|
| `--check` | — | Does the archive still describe the packs? Exit 3 if not. Local only, no network. |
| `--sync` | — | Archive every FULL pack and regenerate its metadata from the live set. |

`EMOJI_ARCHIVE_DIR` is the archive root and `COLLECTION_PACK_BASE` the pack
family it follows; both are required, with no default. Each folder holds:

```
001_logo.png                         your brand logo, copied from BRAND_LOGO_PATH
<slot>_<format>_<key[:12]>.<ext>     one per emoji; slot is 1-based, the logo is 1
_history.json  _history.md           every position, id, content key and glyph
_manifest.md                         name -> current id, the quick lookup
```

`--sync` also rewrites `items.file_path` in the catalog, because that absolute
path is what the roster gallery reads to draw the artwork; dedup is unaffected,
since a `content_key` hashes normalised pixels held in the database rather than
the file. It **renames** anything whose slot moved and regenerates the three
metadata files every run — a recolour mints a new id and a reorder moves slots,
so an archive written once and never revisited stops describing its pack. It
never deletes: a file the live pack no longer knows is reported, not removed.

The `Pack-Archive-Check` Stop hook runs `--check` and blocks the turn when the
archive has fallen behind. Say in the reply whenever you cleared it.

### 12.6 `emojikit/panel.py` — curate web panel

| Flag | Default | Meaning |
|------|---------|---------|
| `--data-dir` | `collection` | Catalog/media directory. |
| `--all` | off | Also show emoji already live in a pack. |
| `--with-pack N` | off | Also show the emoji already live in pack **N**. Repeatable. |
| `--port` | `9450` | Local port. |
| `--preview-fps` | `15` | Frame rate for animated previews. The browser decodes every frame of every animated card that is on screen, so this is the lever on CPU while the grid is idle. |
| `--no-open` | off | Don't auto-open the browser. |

Interactions: **click** a card to toggle include/exclude, **click the
`premium-id:` label** to copy that id to the clipboard (it stops there and does
not toggle the card), **drag** a card to reorder (this is the publish order),
**zoom** with `−` / `100%` / `+`, Ctrl+wheel or Ctrl+plus/minus (Ctrl+0 resets;
the level is remembered). Zooming out packs more emoji per screen and, below
75 %, drops the text rows under the thumbnails; the emoji at the top of the
screen stays at the top of the screen. Each card shows its **grid position** at
the top; the numbers are written from the order every time the grid is
projected, never stored on the card. **The brand logo is
numbered and counted**, because it is the first emoji of every set it leads and
costs one of the 200 (`capacity = per_set - 1` in `build_collection`). Leaving
it out made the panel disagree with what ships — the owner read "200" and the
pack was 201. When the total passes the per-set cap the header says so, with how
many packs it will actually become, rather than letting a second set be a
surprise.

Colour carries the format on the **badge only**: static cyan, animated violet,
video emerald, brand logo amber. The card border is the same for every card —
per-format borders turned the grid into stripes on a dark background — and the
include tick is green, because it answers a different question from the badge
and must not read as the same axis. The header controls each have their own
accent so the row is scannable. Dragging to the top or
bottom edge of the window scrolls the page, so an item can be carried across
the whole catalog in one motion. Releasing anywhere that is not a card cancels
— it used to mean "move to the end".

**A refresh shows the current catalog, and only one panel may hold a port.**
Two separate reasons a reload used to appear to do nothing:

* the page served a snapshot of the catalog taken at start-up, so an emoji
  added by `emojikit/fetch_emoji_ids.py` afterwards was invisible until a restart. The
  view is now re-read from the database on every page load (200 rows, a few
  milliseconds), replacing the shared list **in place** — rebinding it would
  leave every route closed over the old object;
* `socketserver` sets `SO_REUSEADDR` by default, and **on Windows that lets a
  second bind succeed on a port that already has a live listener**. Two panels
  then ran, both logging `Panel at …`, the browser reached whichever socket the
  OS picked, and the older process kept serving its own start-up snapshot —
  which is why only closing the launcher (killing every instance) made a change
  appear. `allow_reuse_address` stays off. B4 identifies an existing panel and
  reopens its session without replacing it; an unrelated port holder is refused.

Editing `emojikit/panel.py` itself still needs the process restarted — a refresh asks the
running server for a page, and that server holds the old code.

**Losing the panel process is never silent.** The page polls `GET /api/ping`
every 5 s. If the panel is gone — or a save is refused — a red banner appears
*and stays* (a toast fades in 2.6 s, which is how an afternoon of reordering
was once done against a dead server and never noticed). The unsaved order is
kept in the page and flushed automatically the moment the panel answers again,
including across a **restart**: the mutation token is per run, so the page
re-reads it from `/` after a 403 and retries. Closing the tab with unsaved work
triggers the browser's "leave site?" prompt.

**Never point automated UI checks at `collection/`.** Reordering is what this
panel does, so a synthetic drag event *is* a write — there is no careful way to
test it against real data. `scripts/panel_sandbox.py` clones the catalog to a
temp directory, serves it on the real panel's port + 1 (imported from
`emojikit.panel.DEFAULT_PORT`, never typed again), and deletes the clone
on exit.

It isolates four things, because cloning the data turned out to be only one of
them.

**The arguments are an allowlist.** `--source`, `--port`, `--all`,
`--with-pack N` and `--bot-username` are accepted; everything else is refused,
including abbreviations (`allow_abbrev=False`). Nothing is forwarded: the
panel's argument list is built inside the wrapper. It used to forward unknown
options straight through, appended AFTER its own `--data-dir`, so
`panel_sandbox.py --data-dir collection` served the live catalog while the
wrapper printed that the live catalog was not served.

**Making it never writes to the source.** The source catalog is opened
READ-ONLY under the ordinary writer lease -- never through `Catalog`, whose
constructor sets WAL mode, creates tables and commits, so cloning an older
catalog used to upgrade it in passing, even when the clone was then refused.
The copy is an online snapshot through `emojikit.sqlite_snapshot`, so committed
rows still sitting in the WAL come too. (A read-only open of a WAL database may
leave an empty `-wal`/`-shm` beside it: that is SQLite's own bookkeeping, not a
change to the catalog.) The CLONE may upgrade its own schema when the panel
first opens it.

**The clone shares no bytes, and they are the right bytes.** Media is COPIED,
never hard-linked, into `media/<hex>.<ext>`, where `<hex>` is the content key's
UTF-8 bytes in hexadecimal: injective, so two keys can never share a file (the
old colon-to-underscore swap mapped `s:a_b` and `s_a:b` to one name), and free
of the colon that opens an alternate data stream on NTFS. A relative
`file_path` resolves against the PROJECT ROOT -- the directory `run.ps1` runs
every tool from -- and an absolute one (the owner's archive) stays absolute;
searching beside the source first used to let a stray file of the same name win.
Each file's SHA-256 is compared before copying, on the copy, and on the source
again afterwards, so a same-length corruption or a file changed mid-copy
refuses the whole clone; comparing sizes let both through. Every `file_path` is
rewritten, and `pack_plan.json` travels with it.

**It refuses rather than half-succeeds.** Another writer, an interrupted
migration, an occupied or overlapping destination, or a media file it cannot
read: each aborts and removes only what that attempt created. A sandbox finished
with one row still pointing at production is worse than no sandbox.

**One sandbox never destroys another.** Each is OWNED from the first
directory it creates to the last file its clean-up removes, by a lifetime lease
held outside it, in `.panel-sandbox-leases/` beside the sandboxes. The lease is
taken before anything is built -- the ownership marker used to be published
first, and a sweep in that gap deleted a finished clone before it was served --
and outlives the directory, because removing a lock file is what lets two
processes each lock a different inode at the same name. Each lease file is a
small permanent tombstone by design. The start-up sweep reclaims a directory
only when its `.sandbox-owner.json` (protocol version 2) names that very
directory AND the lease is free, and it checks the marker again after taking
the lease. Unmarked, foreign, symlinked, still-served and version-1 directories
are left alone: a version-1 owner may still be running under the old scheme,
and an age or a PID is not proof it has stopped. The first sweep matched the
name prefix and deleted every hit, so starting a second sandbox removed the
first one's catalog.

**And it isolates the account.** The panel runs in the wrapper's own process
with `NUMERA_EMOJI_MAPPER_NO_DOTENV=1` -- the flag `load_env()` already honours for the
test suite -- and with every key `.env.example` names, plus anything
credential-shaped, stripped from the environment and restored afterwards.
Without it, a sandbox that had carefully cloned the catalog still called `getMe`
against live Telegram with the real token, because the panel detects its bot
username at start-up and an already-exported token needs no dotenv file. Running
in-process also means killing the wrapper stops the server and drops its lease
together, and no unidentified listener is ever adopted as the sandbox.

This is cooperative test-data isolation, not an OS boundary against hostile code
running as you:

```powershell
.venv\Scripts\python.exe scripts\panel_sandbox.py
```
Animated *and* video emoji play on their own while near the viewport; hover
plays a video only under `prefers-reduced-motion`, where nothing autoplays.
Animated emoji play on their own while near the viewport; the header's
**Animation: On/Off** button stops that everywhere and is remembered in
`localStorage`.

**Order = publish order.** The panel shows items in the saved manual order
(`items.position`). On first open it is seeded to the look-alike similarity
order; after that, drag-and-drop reordering is saved (POST `/api/order` →
`Catalog.set_order`) and drives both the panel and `build_collection` (each
per-format set publishes in this relative order). The brand-logo preview card
is fixed first and is never reordered/counted/saved.

**Performance.** The grid is virtual; only nearby rows exist in the DOM.
Animated previews are native WebP images, with no browser animation library.
Compact zoom caps playback at 10fps and uses 72px previews on ordinary-density
screens; other previews are 104px. `--preview-fps` (1–30, default 15) limits the
server rate. The cache key includes content identity, size and rate, so a tier
the owner has not used yet (compact zoom's 72px, say) starts cold.

**The cache is warmed in the background, and the render bound is resource-aware.**
Measured on this catalog, one animation costs ~155 ms and one video poster
~613 ms; the bound was a hard-coded 2, so a cold tier of 442 animations arrived
two files at a time — about 34 s of rendering delivered in visible chunks while
the owner scrolled. The bound is now `max(2, min(6, cpu_count - 2))`, which on a
16-core machine cut one viewport of 24 cold animations from 2.92 s to 1.55 s
(1.9×) while still leaving most of the machine to the OS and the save handlers.
A daemon thread then renders what the page is about to ask for, in grid order,
so the top of the list is ready first and the scroll meets a warm cache: on a
cold sandbox clone of this catalog it rendered 1505 files in ~2.5 minutes with
the panel fully usable throughout. It is best effort — an unreadable file is
skipped rather than ending the pass, and Ctrl+C never waits for it.

Only genuinely visible animations play: the sticky header's covered region,
hidden tabs and scrolling are excluded. Switching animation off clears moving
image layers and releases video sources; still WebP posters keep video artwork
visible without an idle decoder. Changes affect preview quality, not stored or
published media. Measure performance on a separate catalog with the same media,
viewport, zoom and warm-cache state; desktop load and browser configuration matter.

**Brand logo preview.** `--bot-username YourEmojiBot` selects branding
without a Telegram lookup. Otherwise, if `GENERAL_BOT_TOKEN` resolves to a bot
in `BRAND_LOGO_BOTS` and `BRAND_LOGO_PATH` exists, the panel shows it as a distinct **gold-bordered
first card** labelled "Brand logo (auto-added on publish)" so you can see where
it will land *before* publishing. This card is preview-only: it's not clickable,
not counted in the included/excluded totals, and never sent to `/api/save` — the
logo itself is never part of the catalog and is only actually inserted by
`emojikit/build_collection.py` at publish time (see §12.5). For an unlisted bot, or if the
logo is not configured or missing, the card is simply not shown.

**Emoji already live in a pack are hidden.** The panel arranges the pack being
BUILT, and `is_published` skips a published item at publish time however it is
ticked here — so showing it only invites pruning work that changes nothing.

The rule was "hide only a FULL set" for one round, on the theory that a set
still being filled is still the pack being built. `--new-set` (§12.5) ended
that: a pack can now be left half-empty *on purpose*, so "full" stopped meaning
"finished", and a half-empty published pack kept reappearing in the grid for
the next one. Being published is the property that actually settles it, and it
needs neither a `publish_*.json` nor any capacity arithmetic.

The test is "does this key have a publication row", **not** "does it have a
recorded set name": a row whose `set_name` is NULL is still published, and
reading the name would turn *I do not know where it went* into *it was never
published* — offering a live emoji up to be republished.

The header says how many are hidden; a filter nobody can see is
indistinguishable from having lost the items. They are hidden, never deleted:
those catalog rows are what dedup recognises a re-download by, what maps a
source premium id to ours, and what `sync_order` reads to re-sort an already
published set. `--all` brings them back.

**Each shown pack draws its own brand logo.** Those packs already carry it as
their emoji 0 — it went up when the pack was created — and the logo card opens
that pack's run, so the marker sits above it. One card for the whole grid put
the logo on whichever pack was shown first and left the others looking as though
they had none. Without `--with-pack` the single "auto-added on publish" preview
is unchanged: there the logo is not live yet.

**`--with-pack N` is the narrow version of `--all`.** It un-hides one published
set so a half-full pack can be arranged beside the new candidates going into
it — `emojikit/panel.py --with-pack 5` shows pack 5's emoji and the unpublished ones
together, and nothing else. `--all` is the wrong tool for that job: it also
returns every finished pack, which on a grown catalog is hundreds of cards that
cannot change. The index is resolved through the publisher's own
`publish_*.json`, not by rebuilding `<base><n>_by_<bot>`, because the state file
already records the exact name. Here — and only here — the lookup asks *which*
set an item is in, so a publication row with no recorded `set_name` stays
hidden: unknown-where is not answered with a guess.

**Pack boundaries are drawn in the grid.** A full-width marker carrying the
brand logo heads each pack and is labelled with the grid range it spans, so a
selection that publishes as several packs shows where each one starts and ends.
The splits count **included** items only — an unticked card never reaches
Telegram, so it cannot push the next emoji into the following pack — which is
why ticking recomputes them and not just the counter.

**Where the boundary comes from depends on what the grid holds.** For
candidates it is capacity arithmetic: a new pack every `--per-set` minus the
logo's slot. For emoji that are ALREADY live it is real membership — each such
card knows its pack index, and the marker goes wherever that number changes.
Capacity cannot answer this case at all: two published packs of 95 and 96 are
neither of them a full set, so counting to capacity finds no seam and the grid
would show `--with-pack 2 --with-pack 5` as one unbroken run.

A candidate gets no marker of its own. It used to open a "Not in a pack yet"
run, so every emoji dragged INTO a pack split that pack in two and left a
full-width marker plus the empty rest of its row behind it — and arranging IS
dropping candidates into a pack, so the grid broke exactly while it was being
used. A candidate now continues the run it was dropped into, which is also the
pack it will publish into. The marker is deliberately
a `.packsep` and never a `.card`: the drop handler resolves its target with
`closest('.card')`, and a marker that matched would swallow a drop and silently
do nothing.

The header's **↑ Top / ↓ Bottom** buttons jump to the ends of the grid.

**Dragging shows where the card lands, because the card goes there.** While you
drag, the tile is moved into the slot it would take and drawn translucent with
a dashed outline; releasing just adopts that order. Which half of a tile the
pointer is on decides before-or-after, so the last slot of a row is reachable.
Let go outside the grid, or press Escape, and the card returns to where it
started. It can never be carried ahead of the brand logo.

**Nothing animates while you scroll.** Every card holds frame 0 from the first
scroll event until 180 ms after it settles, and only cards actually in the
viewport animate at rest. A pack of a hundred is mostly animated previews of
30-45 frames each, and scrolling is the one moment that decoding buys nothing.
Switching to another tab freezes them all. `Animation: Off` is still the
lightest the grid can be — nothing decodes at all.

#### Selection mode — move a run in one gesture

Arranging a 200-card pack one emoji at a time is the slow part, so the header
carries a second switch beside **Animation**:

| Gesture | What it does |
|---------|--------------|
| Toggle **Selection** | Reveals an EMPTY pick box on every card. Turning it off drops the picks. |
| Click a card anywhere | Picks or unpicks it — grid card or held card. The whole card is the target, not the box. |
| Shift-click a card | Picks the whole run between it and the last one picked. |
| Click a pick box | Picks or unpicks that one card, exactly once. |
| Drag across pick boxes | Picks the whole run; dragging back **shrinks** it inside the same stroke. |
| Drag a picked card | Carries every picked card together, keeping their order. |

**A check means picked, and nothing else.** An unpicked box is empty. It used to
draw the same check in both states and only change colour, so a box that was not
picked still looked ticked, and the only difference between two states was a
colour. In the tray that also made a multi-selection unreachable in practice:
the box is about 17px on a 64px card, so a click on the card did nothing, and
the drag that carries a whole selection never had a selection to carry.

Clicking the card is safe alongside dragging because a drag emits no click at
all — the two gestures separate themselves by what you did, not by where you
pressed. The grid works the same way now; it used to ignore a click entirely in
selection mode, which left that 1.7em box as the only way to pick on a 140px
card.

**A picked card wears a bright ring that travels around it.** The marker used to
be a flat inset outline, one more dark line among four format accent colours,
and a selection could not be found at a glance. The ring is a conic gradient
rotated by a transform — the compositor animates that without repainting, which
matters on a grid that is virtual precisely because repainting cost 14-17 ms per
pointer move. It stops moving, and stays bright, under `prefers-reduced-motion`.

The pick box is deliberately NOT the tick. The tick says "this ships"; the pick
says "this moves with the others" — and while selection mode is on, clicking a
card no longer toggles the tick, so arranging can never quietly drop an emoji
from the pack. A cancelled group drag puts every carried card back where it was.

### 12.7 `emojikit/emoji_bot.py` — premium-emoji ID extractor bot

No flags. Uses `GENERAL_BOT_TOKEN` + `PACK_OWNER_USER_ID` from `.env`. One
instance at a time (two pollers cause Telegram 409 Conflict). Each reply is a
**single** collapsed quote of `emoji + ID` (per-ID tap-to-copy via `<code>`)
plus `copy_text` “Copy” button(s) underneath — not two quotes (see §8/§19).

**It also runs in reverse: send it ids and it shows you the emoji.** One per
line, comma-separated, `, `-separated, or a single id — all parse. The whole
message must be ids and separators, so a long number inside a sentence (a chat
id, a timestamp) is ignored rather than answered with a wall of placeholders.
Ids are resolved through `getCustomEmojiStickers` first: a `<tg-emoji>` tag
renders the placeholder glyph for an id that does not exist, so an unreported
typo would come back looking exactly like a success. Anything Telegram cannot
resolve is named in the reply.

### 12.8 `coins/` commands

| Command | Purpose |
|---------|---------|
| `coins\fetch_logos.py [pages]` | Download coin logos (CoinGecko) + write `keywords.csv`. `pages` is how many 250-coin market pages to walk (default 40 = up to 10 000 coins). |
| `coins\fetch_paprika.py [--dry]` | Fill remaining coins from CoinPaprika. `--dry` resolves and reports only — no downloads, no pack or map changes. |
| `coins\fetch_cmc.py [--dry]` | Fill remaining coins from CoinMarketCap (needs `CMC_API_KEY`). `--dry` as above. |
| `coins\build_keywords.py` | (Re)build `keywords.csv` from logos on disk. |
| `coins\rebuild_dedup.py [all\|build\|map\|links]` | Default `all` = **delete the old packs** + build + map + links (DESTRUCTIVE); `build` uploads only; `map` re-derives the id map; `links` resends links. |
| `coins\remap_ids.py --emoji-dir DIR [--max-distance N --apply]` | Rebuild `ticker_to_id.json` by image content (drift-proof). `--apply` requires `--max-distance > 0`; needs numpy (`requirements-coins.txt`). |
| `coins\verify_logos.py --emoji-dir DIR [--fix --only a,b]` | Review logos vs official; fix only listed tickers. |
| `coins\check_all_packs.py` | Audit all packs for blank/duplicate stickers. |
| `coins\write_manifests.py --out-dir DIR` | Write per-pack manifest `.md` files. |
| `coins\enhance_map.py` | Map chain-suffixed tickers (e.g. `bnbbsc`) to the base id. |
| `coins\alias_map.py` | Map tickers to a base id by matching coin name. |

### 12.9 `worker/` — Cloudflare Worker (both bots + pack announcements)

Both bots hosted on Cloudflare instead of this machine, plus the endpoint the
local build calls so a finished pack is announced **by the bot** in the channel.

| Route | Auth | What |
|---|---|---|
| `POST /tg/general` | `X-Telegram-Bot-Api-Secret-Token` | Webhook, general bot |
| `POST /tg/coin` | `X-Telegram-Bot-Api-Secret-Token` | Webhook, coin bot |
| `POST /publish` | `Authorization: Bearer …` | Announce finished packs |
| `GET /health` | none | Liveness; returns no secrets |

Each bot has its **own path and its own webhook secret** — the token never
appears in a webhook request, so one shared endpoint could not tell the bots
apart, and one shared secret would let a leak from either forge the other's
updates.

**A token can use `getUpdates` or a webhook, never both.** Registering a webhook
stops `emojikit/emoji_bot.py` (§12.7, §19) receiving anything on that token;
`deleteWebhook` hands it back. Run one or the other per token.

`ADMIN_USER_IDS` **fails closed**, exactly like `emojikit.emoji_bot.allowed_user_ids()`:
unset, empty or all-invalid means the bots answer nobody. Only plain positive
integers count — `Number()` would have accepted `0x10`, `12.5` and `1e3`.
A stranger gets one reply in private and **silence in a group**, so the bot
cannot be turned into a spam vector.

Webhook handlers return **200 even when handling fails**. Telegram redelivers
any non-2xx and every action here is a `sendMessage`, so a redelivery after a
partial success posts the reply twice; failures are logged instead. Nothing is
retried internally, for the same reason as the Python client (§5).

Local side: set `WORKER_PUBLISH_URL` **and** `WORKER_PUBLISH_SECRET` and
`emojikit.build_collection.notify()` routes links through the Worker; leave either unset
and the original direct `sendMessage` path runs unchanged. The duplicate guard
does not move — the per-milestone lists still decide, and a *failed* announcement
is deliberately not recorded as sent, or the guard would skip it forever.
Pack names are validated against `[A-Za-z0-9_]{1,64}` before they reach a public
`t.me/addemoji/` link.

**A trailing set is announced only by a run that finished cleanly** — no failed
upload and no item skipped. A channel link says "this pack is done", and the
end-of-run announcement used to be unconditional, so a run that ended 199 of 200
still posted it. Nothing is lost by withholding it: `state["sent"]` never
records it, so the next clean run announces it, and the log says why it was
held back. A set that filled to capacity *during* the run is announced as it
completes — that one is finished by definition.

**Going up and filling up are two milestones, and each gets its own link.**
`state["sent"]` records the first, `state["sent_full"]` the second; `notify(...,
full=True)` is what the capacity branch calls. One list conflated them, and a
pack announced while it was still being filled then said nothing at 200 — which
is the only moment the channel is waiting for. Pack 2 was announced at 96 emoji
and stayed silent when it reached 200. Neither milestone fires twice, and a pack
that was already announced full before this existed belongs in `sent_full` so
the next run does not repost it.

```powershell
cd worker; npm install
npm run typecheck                    # tsc --noEmit
npm test                             # vitest; fetch stubbed, nothing reaches Telegram
.\scripts\put-secrets.ps1 -DryRun    # which key comes from where; no values shown
.\scripts\put-secrets.ps1            # pipes them from ..\.env into wrangler stdin
npx wrangler deploy
```

`wrangler.toml` has **no `[vars]`**. Everything the Worker reads — both tokens,
both webhook secrets, the publish bearer, the admin list *and the channel* — is
a secret, so none of it is in the committed file. The channel is not a
credential; it is a secret only because `wrangler.toml` is public and both
forms arrive as `env.PACK_LINKS_CHAT_ID` anyway.

`scripts\put-secrets.ps1` reads `.env` with the same parse as
`emojikit.build_pack.load_env` and pipes each value to `wrangler secret put` **through
stdin** — never an argument (arguments are visible in the process list), never
printed. It composes `ADMIN_USER_IDS` from `PACK_OWNER_USER_ID` +
`BOT_ALLOWED_USER_IDS` (deduped, integers only), because that list fails closed
and a hand-typed mistake is silent: the bots simply answer nobody. Missing
webhook/publish secrets are generated and written **back** to `.env`, or the
next run would mint different ones and every delivery would fail its check. A
missing token or channel is an error, not something to invent.

Webhook registration is `scripts\set-webhooks.ps1` — one webhook per bot, read
from the same `.env` the secrets came from so the registration and the deployed
secret cannot drift. A mismatch is silent: Telegram accepts `setWebhook` and
every delivery is then rejected 401, which looks exactly like a dead bot.
`-Status` reports, `-Delete` hands a token back to the poller, and it refuses to
replace a webhook pointing elsewhere without `-Force`.

**Logs.** The channel line follows the Ad Timer Bot's format, bot tag on its own
first line:

```
[general]
❌ ERROR webhook
update 42: sendMessage failed (400): chat not found
2026-08-19 00:45:12 UTC
```

Both bots share this Worker, one D1 table and one channel, so an untagged line
is not worth keeping. The channel is capped at 12 messages a minute; over budget
it drops and counts rather than queueing (a queue in a Worker isolate outlives
its request and loses them anyway), and the count rides on the next message
through. That budget is per isolate, not global. `LOG_CHAT_ID` also accepts a
bare channel id copied from the Telegram UI and adds the `-100` prefix.
**Channel posts from the log channel itself are ignored** — both bots administer
it, so every line posted there came back to both and each wrote another row
about a message we had just written.
D1 is capped at **10 MB, oldest evicted first**, with the insert and the
eviction in one `batch()` — a row cannot be stored without its budget check.
The cap counts stored *text*, not the database file: D1 exposes no cheap
reliable file size, and page overhead plus the index put the file above it.
One line's detail is capped at 2000 characters (a publish announcing 120 packs
listed every name and cost ~6 KB alone). `LOG_CHAT_ID` receives **errors only**
— an unauthorised hit on a public webhook URL is a WARNING, and level-based
routing would let an internet scanner turn that channel into a firehose. Both
bots must administer it; each posts its own lines. Logging is handed to
`ctx.waitUntil()` and every sink failure is swallowed, so it can neither delay
a response nor take a bot down. `GET /health` reports `log_db`.

```powershell
npx wrangler d1 execute emoji-mapper-logs --remote `
  --command "SELECT ts, bot, level, event, detail FROM logs ORDER BY id DESC LIMIT 20"
```

Per-secret detail: `worker/README.md`.

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

### 13.6 Identity migration, recovery and rollback (`scripts/identity_repair.py`)

A video decoder correction can change an item's key and perceptual hash. The
migration updates `items`, `publications`, `seen_files`, publisher state/plans,
and media names together. It never uploads to Telegram.

```powershell
.venv\Scripts\python.exe scripts\identity_repair.py report
.venv\Scripts\python.exe scripts\identity_repair.py migrate-video-keys --apply
```

The canonical data directory owns one native maintenance/writer lock. Catalog
connections, collector storage, publishers (including a first family), panel
saves and archive moves all honor it. Acquisition order is catalog ownership,
existing family locks, then map locks. A `Catalog` must be closed to release its
lease. A busy panel save returns HTTP 503 and retains the browser's queued edit.
Reports take ownership for a consistent view but do not alter catalog/media/state.

`--apply` owns discovery, revalidation, backup, all mutations, final verification
and journal retirement. It first creates a WAL-safe SQLite backup plus a sibling
`catalog.before-video-identity-<stamp>.rollback.json` manifest. The manifest
records original publisher JSON, catalog signatures, byte digests and each
source/destination intent before files change. Media moves use exclusive sibling
hard links followed by removal of the verified source. An identical existing
destination can be reused; unrelated destination bytes are never overwritten.
Filesystems without hard-link support refuse safely with the journal retained.

**After interruption**, run the same `migrate-video-keys --apply` command. The
validated journal is replayed before a fresh survey: a process killed between a
rename and its SQLite path update is recoverable. Source-only, destination-only
and both-present identical states have explicit replay behavior. Ordinary writers
refuse while the journal remains. An unsupported/invalid journal is retained for
inspection; deleting it is not a repair.

**Rollback restores the application snapshot**, including paths and JSON, rather
than only copying the database back. Use the exact bundle path printed by the run:

```powershell
.venv\Scripts\python.exe scripts\identity_repair.py restore `
    --bundle catalog.before-video-identity-<stamp>.rollback.json
.venv\Scripts\python.exe scripts\identity_repair.py restore --apply `
    --bundle catalog.before-video-identity-<stamp>.rollback.json
```

The first command verifies without restoring; the second restores and verifies
media bytes, the complete database (order, inclusion, IDs and publications), and
original state/plan JSON. Later unrelated catalog edits, publisher changes or
replacement media cause refusal. Previously existing identical destinations are
preserved. An interrupted restore resumes with the same restore command. Keep
both the `.db` and `.rollback.json` files; a whole-archive duplicate is unnecessary.
Restoring the old snapshot can make the corrected decoder report pending identity
work again; it restores the prior application version's state, not new identities.

**Legacy migration repair** accepts `--from-backup <catalog.before-...db>`.
Shared immutable FUID/CID evidence must identify one matching row in each snapshot,
and every shared identifier must agree. The current file is then checked against
its current identity. Paths and archive slots never prove identity. Missing or
conflicting provenance leaves required live/in-flight references uncovered and
refuses the repair with those references named.
Only obsolete frozen-plan candidates and skipped history may remain unresolved;
the publisher intentionally skips those absent candidates. History is preserved.

**Exit codes:** `0` verified or clean; `2` usage/missing catalog; `3` pending work
or a verified restore preview; `4` incomplete/refused. Exit 4 after interruption
can mean partial application, with the journal and rollback bundle retained.
Missing media, decode failures, stale required references, orphaned identifiers,
collisions or unresolved operations never produce a `Verified` success.

`SUSPECT MAPPINGS` remains a heuristic over historical near matches. It does not
validate historical FUID/CID bindings: an old file_unique_id alone cannot retrieve
its source bytes from Telegram, and zero suspects is not proof of past correctness.

**Refused storage collisions** retain the complete incoming file under
`<data-dir>/media/<format>/quarantine/`, outside per-run scratch cleanup. A JSON
record preserves its checksum, size, attempted destination and available source
identifiers. Repeating the same refused payload reuses its retained copy. Existing
destination bytes and catalog bindings remain intact for deliberate recovery.

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
| `to_static_png(src, out)` | Path | Any image (SVG via resvg) → 100×100 PNG. |
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

A **video input is decoded with an explicitly named alpha-capable decoder**,
placed before `-i`: `libvpx-vp9` for VP9, `libvpx` for VP8. Both of those
formats keep alpha in a separate WebM layer that ffmpeg's *default* `vp9`/`vp8`
decoders drop without a word, so the filter chain would see no alpha and the
transparent pad would land on an opaque frame — a re-encoded transparent emoji
came out a black square. The decoder is chosen from the codec `probe_video`
reports, never from the file extension: `.webm` is a container and says nothing
about what is inside it. GIF/PNG inputs get no override. The same choice is
needed to *inspect* a clip — probing a VP9 emoji with the default decoder
reports every one of them as opaque, correct ones included — which is why the
decision lives in one module (§14.2) that both the converter and the identity
layer call.

### 14.2 `emojikit.video_decode`

One module, because "which decoder reads this file's alpha" was previously
answered in two places and they disagreed — `fingerprint()` and
`perceptual_hash()` returned different keys for the same clip.

| Function | Returns | Notes |
|----------|---------|-------|
| `decoder_available(name)` | bool | Is this ffmpeg decoder built in? Cached per name. |
| `decoder_args(path)` | `["-c:v", …]` or `[]` | The decoder for this file's **probed** codec. Raises `UndecodableVideo` when alpha fidelity cannot be ESTABLISHED; `[]` only on positive evidence that no override applies. |
| `frames_rgba(path, fps=SAMPLE_FPS)` | bytes | 64×64 RGBA frames at `fps`, alpha intact. `SAMPLE_FPS` (10) is the IDENTITY stream and is frozen — every stored content key hashes it. |
| `first_frame_bytes(path)` | bytes \| None | The first of those frames. |

`frames_rgba` is memoised on `(path, size, mtime_ns)` with a small bounded LRU.
`same_image` used to decode the same file up to six times per comparison; the
cache cut the video identity suite from 163 s to 48 s, and shortens real ingest
by the same mechanism. Keying on mtime and size — not the path alone — is what
keeps a re-encoded file from answering with its old frames.

### 14.3 Comparing two videos

`same_image(a, b, "video")` walks the timeline. It used to read frame zero and
nothing else — both halves of its two-agreement rule went through
`_first_video_frame` — so two real clips sharing ten opening frames compared
equal, and reconciliation bound the foreign clip's ids to our catalog item.

Two rules make it sound:

* **Every sampled frame pair must agree**, under the same structure-and-colour
  rule a static image gets. There is no averaging across frames: an average is
  exactly what lets one differing segment hide. A length difference beyond one
  frame of slack is a different clip, however well the opening matches.
* **Content-key equality is not proof, for video.** The key hashes the 10 fps
  identity stream, and a change shorter than one sampling interval falls between
  its frames — two clips differing in exactly ONE frame (flat blue against flat
  green, premultiplied mean delta 100.5) hash to the same key. The comparison
  therefore samples at `VIDEO_COMPARE_FPS` (30, the rate this project's own
  encoder produces) and never short-circuits on the key.

Failing closed stays the contract: a clip that cannot be read is `None`
(undecidable), never `False`.

### 14.4 `emojikit.repaint`

Baking Telegram's `--tint` into artwork, rather than publishing a set that asks
the client to flatten it. Split out of `media.py` (a different job, and that
file had reached the size ceiling), and re-exported from it so existing callers
are unchanged.

| Function | Returns | Notes |
|----------|---------|-------|
| `parse_tint(text)` | `(r, g, b)` | `#rrggbb`, `#rgb`, or `r,g,b`. |
| `repaint_in_place(path, fmt, rgb)` | bool | Recolour a Lottie or a static in place; video is refused. |

Two shapes it must not skip, both of which used to return `True` having changed
nothing: an **animated colour** is a list of keyframe objects with `s`/`e`
rather than a flat `[r,g,b,a]`, and a **gradient** packs its colour stops
`[offset,r,g,b] × p` followed by its opacity stops `[offset,alpha]` in one flat
array — so `len // 4` as the stop count overwrites the opacity ramp with colour.
`g.p` is the stop count and is honoured when present.

### 14.5 `emojikit.catalog.Catalog`

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

### 14.6 `emojikit.logsetup` (advanced logging)

`setup_logging(name, *, console_level=INFO, file_level=DEBUG, color=None)`
configures a console handler plus a fresh UTC file log under `logs/`, named
`<name>_YYYY-MM-DD_HH-mm-ss_UTC_<run_id>.log`. It is idempotent per process and
returns the root logger. Capabilities:

- **Per-run id** — an 8-hex id stamped on every file line.
- **Automatic secret redaction** — known secret *values* (auto-registered from
  `TELEGRAM_BOT_TOKEN`, `GENERAL_BOT_TOKEN`, `CMC_API_KEY`, …) and token-shaped
  strings/`/bot<token>/` URLs are masked in **every** record, including
  exception tracebacks. Content hashes (`s:...`) and numeric ids are **not**
  redacted. Register extra secrets with `register_secret(value)`.
- **Rich file format** — `[UTC] [LEVEL] [run_id] [logger] module:line message`;
  concise (optionally ANSI-colored on TTY) console format.
- **Uncaught-exception capture** — `sys.excepthook` + threading hook log full
  tracebacks as CRITICAL, and the traceback printed to stderr is redacted too
  (delegating to the default hook would have printed the token raw).
- **Quiet third parties** — `urllib3`/`requests`/`PIL` turned down;
  `logging.captureWarnings(True)`.
- **End-of-run summary** (atexit) — `run <id> finished in N.NNs | warnings=… errors=… critical=…`.

The public surface is exactly three names:

```python
from emojikit.logsetup import setup_logging, redact, register_secret
setup_logging("myscript")
print(redact(some_text))        # mask before any manual print
```

Secret handling is covered by `tests/test_logsetup.py`, which also fails the
build if any value from `.env` appears in a git-tracked file.

### 14.7 `emojikit.build_pack.Telegram`

Thin Bot API client (used everywhere). Key methods: `get_me`, `send_message`,
`get_sticker_set`, `download_file`, `create_emoji_set`/`add_emoji` (format-aware,
for static/animated/video), and the legacy static `create_set`/`add_sticker`.
`_call(method, data=, files=)` handles flood waits (`retry_after`) and the
~2-minute `STICKERSET_INVALID` name-release delay automatically.
---

## 15. Media formats deep-dive

### 15.1 Static (`static`)

- File: PNG or WEBP, **exactly 100×100**, RGBA (transparent background).
- Built by `emojikit.make_emoji_pngs._fit_100` / `media.to_static_png`: trim fully
  transparent borders, scale to fit 100×100 with LANCZOS, center on a
  transparent canvas. SVG sources are rasterized by resvg straight to RGBA
  (gradients included).
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
- In the panel, `.tgs` is rendered server-side to an animated WebP
  (`/preview/<key>`) and played natively by the browser as an `<img>`.

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
  **A single-frame video is shorter than one sampling interval and yields no
  frames**, so an empty resample retries at the file's own frames rather than
  falling through to a byte hash — which is not a content key, and which made
  two containers of the same frame fail to dedup while a `-c copy` remux moved
  the key (WebM randomises its SegmentUID). `fingerprint()` shares this exact
  decode; it must never grow its own copy again.
- **animated** → `"a:" + sha256(<canonical Lottie JSON>)`. The `.tgs` is
  gunzipped, parsed, re-serialized with sorted keys; re-gzipped copies match.
- unknown → `"r:" + sha256(raw bytes)` — reached only when ffmpeg renders
  nothing at all. A ffmpeg **failure or timeout** is not that case: it raises
  `MediaError` and the ingest site fails that one item, because a byte-hash key
  looks valid, never dedups, and hides the timeout.

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
16×16 **signature** (RGB on black, plus the alpha channel as a fourth plane —
see below for why alpha is not optional), and match each local source logo
(`<ticker>.png`) to the live sticker whose signature is nearest (L2 via a
chunked Gram matrix). Drop matches above `--max-distance` (coins never
uploaded). This is the canonical way to (re)build the map; a correct run scores
100% against the live stickers (`--max-distance 200 --min-margin 0` is the
calibration that has worked in practice — recheck the distance histogram the
dry run prints before trusting it on a changed corpus).

```powershell
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "<coin logo folder>" --max-distance 200 --apply
```

A resumable cache (`coins/remap_live_cache.json`, gitignored) avoids
re-downloading.

Things `remap_ids.py` does **not** do for you, each learned the hard way:

* It writes only tickers that have a `<ticker>.png`, so letting it replace the
  map outright **deletes every alias** that has no file of its own (`1inchbsc`,
  `avaxc`, …). Re-derive them before writing, either from a sibling that shares
  the old map's id, or — for an alias that was never in any prior map, such as
  a chain-suffixed ticker added straight to the inventory — via
  `coins._inventory.base_ticker()`, the same resolver `alias_map.py` and
  `enhance_map.py` use.
* **The signature must carry shape, not just colour.** RGB-on-black alone is
  blind to any logo drawn in black on transparency: it flattens to a uniformly
  black square, so Aptos/Arkham/NEAR/Worldcoin/Bittensor and every other
  black-wordmark coin measured pairwise distance **0.0** — indistinguishable
  from each other. That produced a confident, entirely wrong diagnosis once
  ("128 coins have the wrong logo file"; they did not — the files were fine,
  the metric was blind). **A distance of 0.0 between two files is a claim about
  the metric before it is a claim about the files** — look at the images before
  trusting the number. `SHARED_GROUP_LIMIT` (20) still earns its place as a
  safety net for a *genuinely* corrupt or duplicated source file; just don't
  assume that is the only thing it can mean.

Take your own dated backup first. `ticker_to_id.prebroken.json` is **not** one —
it is drifted too, and scores the same as the map it was meant to repair.

### 17.3 Preventing drift in new builds

Both `coins/rebuild_dedup.py` and `emojikit/build_collection.py` now record the **actual
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
.venv\Scripts\python.exe -m emojikit.fetch_pack https://t.me/addemoji/SomePack_by_bot --token-env GENERAL_BOT_TOKEN
#    -> "Pack SomePack (...): 80 stickers -> new=80 dedup=0 failed=0"

# 2) Open the curate panel and untick anything you don't want, then Save:
.venv\Scripts\python.exe -m emojikit.panel
#    (browser opens http://127.0.0.1:9450/ ; click cards / Shift+click ranges / Save)

# 3) Preview the publish plan:
.venv\Scripts\python.exe -m emojikit.build_collection --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN --dry-run
#    -> "static: 76 emoji -> 1 set(s) named mypacks1_by_<bot> ..."

# 4) Publish (resumable). Each finished pack DMs you its addemoji link:
.venv\Scripts\python.exe -m emojikit.build_collection --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN
#    -> manifests written to collection/manifests/mypacks1_by_<bot>.md
```

### 18.2 Find which pack an emoji id belongs to, then download it

```powershell
# Resolve id -> set_name (uses getCustomEmojiStickers), then fetch that set:
.venv\Scripts\python.exe -c "import os,sys,json;sys.path.insert(0,'.');from emojikit.build_pack import Telegram,load_env;load_env();tg=Telegram(os.environ['GENERAL_BOT_TOKEN']);print(tg._call('getCustomEmojiStickers',data={'custom_emoji_ids':json.dumps(['<ID>'])})[0]['set_name'])"
.venv\Scripts\python.exe -m emojikit.fetch_pack <set_name> --token-env GENERAL_BOT_TOKEN
```

### 18.3 Build a video emoji pack from GIFs

```powershell
# ffmpeg must be installed. GIFs become VP9 .webm video emoji.
.venv\Scripts\python.exe -m emojikit.add_media --in input\my-gifs --emoji 🔥
.venv\Scripts\python.exe -m emojikit.build_collection --base myvid --title "My Animations" `
    --token-env GENERAL_BOT_TOKEN --formats video
```

### 18.4 Rebuild the coin id map after any pack change

```powershell
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "<coin logo folder>" --max-distance 200 --apply
.venv\Scripts\python.exe coins\enhance_map.py
.venv\Scripts\python.exe coins\alias_map.py
.venv\Scripts\python.exe coins\write_manifests.py --out-dir "<coin archive folder>"
```
---

## 19. The Numera Emoji Mapper bot internals (`emojikit/emoji_bot.py`)

Long-polling loop: `getUpdates(offset, timeout=50, allowed_updates=[message,
channel_post, edited_channel_post, my_chat_member])`. Each update is dispatched
to `handle_update`. Errors in one update are caught and logged so the bot never
dies on a single bad message.

Pure, unit-tested helpers:

- `extract_custom_emoji_ids(message)` — ordered, de-duplicated `custom_emoji_id`s
  from `entities`, `caption_entities`, **and `quote.entities` /
  `external_reply.quote.entities`**. The `quote` field (Bot API `TextQuote`) is
  set when the user manually quotes part of a message they're replying to; per
  the Bot API, only bold/italic/underline/strikethrough/spoiler and
  **custom_emoji** entities are preserved inside that quote. Without scanning
  `quote.entities`, premium emoji inside a quoted reply were silently dropped
  (only entities on the reply's own text were seen). Repeats (same id, whether
  inside the quote, the reply text, or both) are de-duplicated to one occurrence.
- `build_payloads(ids, labels, rich=True)` — returns a **list of
  `(html_text, inline_keyboard)`** payloads. Each message has a single collapsed
  `<blockquote expandable>` where every line is
  `<tg-emoji emoji-id=id>fallback</tg-emoji> <code>id</code>` when `rich`
  (renders the **real premium emoji**); each `<code>` is tap-to-copy (mobile).
  Copy is provided by `copy_text` **“Copy”/“Copy a-b” inline button(s)**
  (`_copy_keyboard`) covering that message's ids; Telegram caps each button at
  256 chars (~12 ids), so a message with more ids gets several chunked buttons
  that together cover all of it.
  `_batch_ids` splits the id list by the **message**-length budget
  (`MSG_MAX`=3500 chars, mode-independent ~110 chars/id) so as many ids as
  possible share one message — batching by the smaller button limit was tried
  and reverted because it fragmented a 50-id reply into 5 short messages instead
  of ~2; rich/plain renders still align 1:1 batch-for-batch. Multi-message
  replies are labelled "part i/n". `send_reply` sends the rich version and, if a
  message is rejected (a custom emoji the bot can't render), automatically
  re-sends that message with `rich=False` (fallback chars only).
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

The same extraction is ported to TypeScript in `worker/src/emoji.ts` (§12.9),
including the `quote` / `external_reply.quote` entities — among the few that
survive into a partial quote, and missing them silently drops premium emoji
inside quoted replies. **A token can serve `getUpdates` or a webhook, never
both**: point a webhook at the Worker and this poller goes deaf on that token.

Operational notes: run exactly one instance (two concurrent `getUpdates` cause
**409 Conflict**). For groups the bot needs admin or privacy-mode off to see
messages; for channels it must be an admin to receive `channel_post`.

---

## 20. The Curate panel internals (`emojikit/panel.py`)

A `ThreadingHTTPServer` on `127.0.0.1`. Routes:

| Route | Response |
|-------|----------|
| `GET /` | The page (`assets/panel.html`: markup, CSS, items embedded as JSON, the per-run values). |
| `GET /img/<key>` | The media bytes (webp/png/webm) with correct MIME. |
| `GET /preview/<key>?fps=N&size=S` | Cached animated WebP for TGS; `still=1` returns a still for static/video/TGS. Sizes: 52, 72 or 104; rates: 1–30, bounded by `--preview-fps`. |
| `GET /static/<file>` | Static assets (logo, favicon, and the panel scripts, `emojikit.panel.SCRIPT_FILES`), traversal-guarded. The scripts are requested as `panel-grid.js?v=<hash>` — the hash is the scripts' content (`emojikit.panel.ASSET_VER`), because the route is immutable-cached and an edited script would otherwise be served stale. The query is stripped before the file lookup. |
| `POST /api/save` | Body `{"excluded":[keys], "known":[keys]}` → `catalog.set_inclusion(...)`, **restricted to `known`**. |
| `POST /api/order` | Body `{"order":[keys]}` → `catalog.set_order(...)` (drag-to-reorder = publish order). |
| `POST /api/client-log` | Up to 32 whitelisted events, 16KiB per batch; counts/revisions/status/error type and location only. No token, label, media ID or arbitrary message fields. |

All POST routes are guarded: loopback-only `Host`/
`Origin`, a per-run token sent as `X-Panel-Token`, an exact-permutation check on
the order, a content-type check and a body cap (`tests/test_panel.py`).

**`known` is the save's scope, and it is mandatory.** A save carries the FULL
selection — every key it does not name becomes *included* — so a request with no
notion of scope speaks for the whole catalog. The panel re-reads the catalog on
every page load, so a tab opened before `emojikit/fetch_emoji_ids.py` added an emoji, or
before the owner deselected one in a second tab, held a stale snapshot; saving
from it silently re-included rows it had never seen and answered `{"ok": true}`.
The request now states which keys it was showing, and the decision is applied
only inside that scope (intersected with what the catalog currently holds, so a
key that has since vanished is harmless). A page too old to say gets **409** and
a "reload the page and save again" message: one reload costs a second, while a
silent re-inclusion is invisible until a publish ships the wrong pack.
`tests/test_panel_save_scope.py` drives all of it through the real handler.

Ordering: `emojikit.panel_view.order_by_similarity` groups items by format (static, then
video, then animated) and within each runs a greedy nearest-neighbour walk on
the perceptual hash so look-alikes are adjacent. Items without a hash (animated)
keep content order. The view model lives in `emojikit/panel_view.py` — `build_view`,
`order_by_similarity`, `packs_named`, `copy_id_for` — with `emojikit/panel.py` left
holding the server. Pure functions on one side, sockets on the other.

Front-end:

- Dark OLED theme, neon-blue accent (`#22d3ee`), Inter font, big 108-px cards
  with a label and a format badge. Thumbnails sit on a **dark-slate contrast
  checkerboard** (`#828c9a`/`#464e5a`, matching the dark theme) so black,
  hollow-center, and faint/low-opacity emoji are all visible by
  default, plus a **Backdrop switch** (Checker → Light → Dark → Gray, persisted
  in `localStorage`) to inspect tricky emoji on any background.
- All selected by default. Click toggles; **Shift+click** toggles a range.
  Header buttons: ↑ Top / ↓ Bottom / Select all / Deselect all / Invert /
  Backdrop / Animation: On|Off / Save. The two jump buttons scroll the
  **document**, not `scrollIntoView`: that aligns an element with the top of
  the viewport, which sits behind the sticky header, so Top stopped a
  header-height short — hiding the Pack 1 marker — and Bottom stopped short for
  the same reason. Select all / Deselect all / Invert are mode-dependent: while
  selection mode is on they drive `picked` (`pickAll`/`clearPicked`/
  `invertPicked`), otherwise `included` (`setAll`) — before this they only ever
  touched `included`, so picking several emoji looked broken because nothing
  on screen responded to them.
- **Selection and history** (`assets/panel-holding.js`). Selection mode hides
  the inclusion tick and shows only the pick control. Shift-click picks the
  inclusive range from the last anchor, skipping logos and held cards.
  Snapshots include order, inclusion, picks, hold origins and view settings.
  One drag or pick stroke creates one undo entry; cancelled drags restore the
  original snapshot. **Reset all** restores the last successful explicit Save
  in this page and is itself undoable. A Save captures its checkpoint before
  sending, so a later edit is never folded into an older acknowledgement.
- **Holding area.** Held cards stay in the ordering model but occupy no grid
  slots. **Unhold** restores the original slot; **Unhold all** checks every
  destination before changing any card. A full original pack refuses with a
  capacity message. The grid drop commits a tray card's inclusion before
  recording history; `dragend.dropEffect` is not used as proof of a successful
  drop. Tray thumbnails are still images and are reused across updates.
  The tray joins **selection mode**: each held card carries the same pick box a
  grid card does, click plus shift-click takes a run of them, dragging one
  picked card carries every picked held emoji, and **Unhold** on a picked card
  returns the whole picked set. Without it a held emoji was pickable nowhere —
  the pick box lives on a grid card and a held emoji has none — so the tray was
  the one place selection mode could not reach.
- **An emoji dropped into another pack joins it.** The panel states the
  INTENDED layout; it is not a mirror of what is live on Telegram. Drag an
  emoji into the pack you want it to end up in, save, and the emoji is
  re-stamped into that pack — the grid redraws with it inside the destination
  run. Nothing moves on Telegram at that moment and nothing can: the Bot API
  has no move-between-sets call, so a real move is a delete plus a re-add that
  mints a new `custom_emoji_id`. **Save writes the decision to
  `<data-dir>/pack_plan.json`** (§ "The move plan" below) and the real
  rearrangement is a separate, deliberate step performed from that file.
  The re-stamp is the load-bearing half: without it `packStarts()` re-groups
  the card by the pack number it arrived carrying, it becomes a one-card run
  still labelled with the pack it came from, and the drag reads as having
  snapped back — which is exactly how this was first reported.
  **The pack comes from the card the pointer was aimed at**, recorded while the
  drag is live, never from whatever ends up above the card once it lands. Those
  two answers differ at every pack boundary, and the difference was measurable:
  a held emoji dropped on pack 4's FIRST card landed stamped pack 3. Every drop
  re-stamps, from the tray and from inside the grid alike — a grid-to-grid drag
  used to keep its old pack number and cut the destination pack in two. A drop
  that never passed over any card stamps nothing: an unknown destination is not
  a destination. A drop into a run that has no pack number yet leaves the emoji
  without one, so it continues that run instead of starting a false one.
- **A full pack takes nothing, and the holding tray is how you make room.**
  Packs are fixed 200-emoji buckets, logo included. A drop into a pack already
  at 200 is refused with `Pack N is full (200/200). Hold one of its emoji
  first, then bring this one in.` — park one of that pack's own emoji in the
  tray, then bring the replacement in. Capacity is asked of the DESTINATION,
  not of the emoji's own pack; asking the wrong one refused moves OUT of a full
  pack and allowed moves INTO one.
- **Pack boundaries and numbers** count included cards and the brand logos.
  Holding four entries from a full pack changes `#1–#200` to `#1–#196`, including
  when that pack is followed by other published packs. The same visible-index
  projection controls virtual rows, card positions and separator ranges.
- **Save queues** (`assets/panel-actions.js`). Orders auto-save; inclusion
  waits for explicit Save. Each queue permits one request at a time and keeps
  its newest body. A 400/409 rejection blocks only that body; a newer body can
  progress and still retry a transient failure. Debounce, retry and heartbeat
  share the same eligibility deadline. Dirty state and unload protection remain
  until the corresponding state is acknowledged; the warning offers a local
  JSON draft export for conflict recovery.
- **UI logs** (`emojikit/panel_logging.py`). Browser events are batched at most
  once per second, with a three-second delivery deadline and a 32-event buffer.
  The authenticated endpoint validates the complete batch before logging and
  caps request frequency. UI actions and error locations go to the panel's
  normal UTF-8 UTC log; arbitrary exception text and private content do not.
  Logging failure does not enter or block either save queue.
- **The grid is virtual** (`assets/panel-grid.js`). `ITEMS` is the order and
  the selection; the DOM holds only the rows within half a screen of the
  viewport, between two spacers that carry the height of everything above and
  below. Every card is the same fixed height and every row offset is integer
  arithmetic on numbers JavaScript computes and hands to CSS as variables
  (`--cols`, `--cardH`, `--sepH`, `--gap`, `--z`), so a render is a binary
  search plus ~100 node moves, whatever the catalog holds. Measured on the
  1 063-card catalog in headless Chromium: a drag step cost 24–46 ms per
  pointer move on the old page (every one re-laid-out 1 062 cards; 40 % of
  frames over 32 ms) and 16–24 ms here with none over 32 ms; a cold scroll
  sweep spent 802 ms in long tasks before and 113 ms after, and the 1.8 s hang
  the old page showed on the way back up is gone. 11 743 DOM nodes became 714.
- **Zoom** scales the card: everything inside a card is sized in `em` off one
  `font-size: calc(12px * var(--z))`, and the column count follows from the
  container width, so `--z` changes both how big a tile is and how many fit.
  Below `0.75` the body gets `compact` and the text rows are dropped. The
  item at the top of the screen is re-scrolled to the top after the relayout.
  Range 0.4–2.4, steps of ×1.15, persisted as `panelZoom`. Ctrl+wheel and
  Ctrl+plus/minus/0 are intercepted (`passive:false`) so the browser's own page
  zoom does not fire as well. `zoomReset` is a typeable `<input>`, not a
  reset-only button: Enter parses and applies a percentage, double-click still
  resets to 100 %. `paintZoom()` writes `.value`, not `.textContent` — the
  latter is silently inert on an `<input>`, which is exactly why the click
  handler that used to live there moved to `panel-holding.js` instead of
  growing in place.
- **Drag edits the model as you drag** (`assets/panel-actions.js`). Each
  `dragover` that changes the target moves the carried items inside `ITEMS`
  and re-projects the grid, so the translucent tile IS where they land; the
  drop only records the snapshot taken at `dragstart` as history and saves; a
  cancel restores that snapshot. There is no index arithmetic at drop time.
  A carried card whose row scrolls out of the window is **parked** in a hidden
  holder, never removed: the browser delivers `dragend` to the source node,
  and a removed source leaves the gesture stuck.
- **Static and animated are both plain `<img>`** — the browser owns decoding
  and compositing. Not `loading="lazy"`: a card only exists once its row is
  within half a screen, so the window is the lazy loading. Video is
  `<video preload="metadata">` (muted, looping, `playsinline`) whose **source
  is attached only while animation is on and the card is in the viewport** and
  released when it leaves (`videoIO`): a media player is the most expensive
  thing a card can create or tear down, and 56 of them alive at once was what
  the old page paid on every load. A card that is unmounted releases its
  player the same way.
- **One `IntersectionObserver` (no margin) gates playback**: it swaps an
  animated card's `src` between the still (`?still=1`) and the animated WebP,
  and plays/pauses `<video>`. A grid of *everything* playing was the original
  CPU sink; the fix is the viewport bound, not hover — a grid of frozen stills
  cannot be curated, which is why hover-only was rejected for both. Scrolling
  freezes every card on frame 0 until it settles.
- `prefers-reduced-motion` is respected: nothing plays by itself, and hover
  becomes the only way to play a video, which is what those handlers are for.

The panel ships no animation library: animated emoji are rasterised to WebP by
`rlottie-python` on the server, so the page needs nothing from a CDN and works
offline.

### The move plan (`<data-dir>/pack_plan.json`)

The panel decides nothing on Telegram. It is where the owner says what the
layout **should** be; every save writes that decision to `pack_plan.json` beside
`catalog.db`, and rearranging the live packs is a separate, deliberate step read
from that file.

It has to be written explicitly rather than inferred from the order, because
with fixed 200-emoji buckets a pack boundary does not follow from position:
"which pack was this meant for" is a decision, and a decision that is guessed
later is a decision nobody made.

```json
{
  "version": 1,
  "written_utc": "2026-09-19T02:07:19Z",
  "per_set": 200,
  "targets": [["s:e8dc…", 3], ["s:5cb9…", 1]],
  "known":    ["s:5cb9…", "s:c0ff…", "s:e8dc…"],
  "excluded": ["s:c0ff…"],
  "counts": {"1": 199, "2": 197, "3": 199},
  "logo_slots": {"1": 1, "2": 1, "3": 1},
  "over_capacity": {},
  "moves": [{"key": "s:e8dc…", "label": "refx-nexus-3-round",
             "from_pack": 2, "to_pack": 3}],
  "held":  [{"key": "s:c0ff…", "label": "premium-id:5447…", "from_pack": 3}]
}
```

| Field | What it is |
|-------|------------|
| `targets` | **The authoritative intent**: every emoji the panel has an opinion about, paired with the pack it should end up in. The server reads this back on load, so a saved move survives a reload. |
| `moves` | The subset of `targets` whose intended pack differs from the one the emoji is live in — the work, derived for the reader's convenience. `targets` is the source of truth; `moves` is what it means today. |
| `held` | Every emoji parked in the tray, with the pack it came out of. Not a move — the owner took it out and has not said where it goes; parking one is how room is made for an arriving emoji. |
| `known` | Every key any save has spoken for. A page shows only part of the catalog, so this records the scope decisions were made in and lets a later partial save merge instead of overwrite. |
| `excluded` | Every key currently held out of a pack, across all saves — not only the ones the last page could see. |
| `counts` | Emoji per pack in the intended layout. |
| `logo_slots` | Packs whose brand logo consumes one of the 200 slots. The logo is emoji 0 of **each** pack, not once per family, so capacity has to count it per pack. |
| `over_capacity` | Packs whose `counts` plus `logo_slots` exceed `per_set`. Normally empty, because a drop over the cap is refused while curating — but a plan read back later must be able to say so rather than look healthy and fail at publish. |

A **partial save merges**: a page that can see only some of the catalog replaces
the decisions inside its own scope and keeps every other decision in the file.
That is why `targets`, `known` and `excluded` are whole-catalog while `moves`
and `held` read as a to-do list. A plan written by an older panel that carries
only `moves` is still understood — its moves are read as the intent.

The one thing a save does drop is a decision about an emoji the catalog no
longer holds. The keys are checked against the database under the same lease
that writes the plan, so a deleted emoji stops being counted and stops
reserving a slot; without that the file would only ever grow, and a pack could
report itself over capacity because of emoji that are gone. A pack nobody
targets any more loses its `logo_slots` entry for the same reason.

An unreadable `pack_plan.json` is never silently replaced with an empty one: the
panel refuses to load and names the file, because discarding saved intent to
keep the page opening is the worse failure. Repair or move the file, then
reload.

An emoji the page carried no pack for is **absent** from the plan entirely:
saying nothing and saying "leave it" are different claims, and only the first
is true. A candidate that was never published has no `from_pack`, so it is
counted but never a move — there is nothing to move it out of. The brand logo
is skipped: it is emoji 0 of every pack and does not travel.

The file is written atomically, because the step that reads it may start at any
moment and a half-written plan is a scrambled instruction set.

---

## 21. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `GENERAL_BOT_TOKEN not set` / `getMe failed: Not Found` | Token missing/invalid in `.env`, or a stale shell env var shadowing it. | Put a valid token in `.env`; in PowerShell clear a leftover var: `Remove-Item Env:\GENERAL_BOT_TOKEN`. |
| `ffmpeg not found` | ffmpeg/ffprobe not on PATH. | `winget install Gyan.FFmpeg` (only needed for video). |
| `cannot find ft2build.h` / reportlab build fails | An old checkout still pinning `reportlab<4`, which had no cp312 wheel. | Pull latest: the reportlab/svglib stack was replaced by `resvg-py`. |
| `Python int too large to convert to SQLite INTEGER` | Old catalog code stored an unsigned 64-bit phash. | Fixed in `emojikit/catalog.py` (signed storage); pull latest. |
| Fetch reports `dedup` on a pack with no real duplicates | Near-dup merging was on. | It's **off by default** now (`--phash-threshold -1`); pull latest or pass `-1`. |
| Panel images 404 | `/img/<key>` not URL-decoded. | Fixed; pull latest. |
| Bot replies nothing in a group | Privacy mode on / not admin. | Make the bot admin or disable privacy mode in BotFather. |
| Bot stops with **409 Conflict** | Two `getUpdates` consumers. | Run only one `emojikit/emoji_bot.py` instance. |
| `STICKERSET_INVALID` right after deleting a set | Telegram locks a freed set name ~2 min. | The client auto-waits and retries; just let it run. |
| `flood wait Ns` | Telegram rate limit. | The client honors `retry_after` automatically; let it continue. |
| Animated card shows ▶ but never plays | Bad/non-standard `.tgs`, or JS blocked. | Other cards still work; that single TGS just won't render. |
| Wrong logo for a coin | Source logo is a different token with the same ticker. | `coins\verify_logos.py --fix --only <ticker>` with the correct image. |
| Scrambled ticker→id map | Position-based mapping (legacy). | Rebuild with `coins\remap_ids.py ... --apply`. |
| `canonical_map.lock is held by pid …` | Another coin tool is mid-write of `ticker_to_id.json`. Every writer holds one lock across the whole read-modify-write, so neither can lose the other's ids. | Let the other tool finish, then re-run. The map was **not** modified. |
| `publish_<base>.lock is held by pid …` **and that process is gone** (you stopped a publish, or it crashed) | The lock outlives the run that made it. It is reclaimed once the holder is provably dead AND the lock is over two minutes old — the delay covers the moment between a lock being created and its owner being written into it. | Wait ~2 minutes and re-run the same command. It resumes where it stopped; nothing is re-uploaded. |
| `the pending replacement … was recorded against map A, not this run's map B` | A `verify_logos --fix` was interrupted; its intent is bound to the exact `--map`/`--state` it started against. | Re-run with the **original** `--map`/`--state` so it can be resolved, or review the pack and delete `coins\verify_logos_intent.json` deliberately. |
| `… exists but its first sticker is not <x>.png; refusing to adopt` | A set of that name exists but this run did not create it (leftover family, or someone else's). Existence is not identity. | Rename the family, or delete the stale set, then re-run. |
| `position N could not be examined …` from `build_collection` | A live sticker could not be downloaded or hashed, so the publisher cannot tell whether it is one of ours. Guessing "not ours" is what publishes a second copy. | Usually transient — re-run. If it repeats on **video or animated** sets, ffmpeg is off PATH: content hashing needs it, and without it every such sticker is unexaminable. Install ffmpeg (see Prerequisites). |
| `Bad Request: wrong file type` on an **animated** item | Telegram's **uploader** refuses a subtract mask (`masksProperties[].mode == "s"`); its **player** renders one happily. So a sticker can be live in a published pack for years and still be refused when you upload the same bytes -- proven by downloading one from a live pack and sending it straight back untouched. Add masks (`"a"`) are fine. Nothing local can see it: the file is valid gzip, valid Lottie, 512x512, in-spec fps and duration. | `validate_tgs` now refuses it at ingest and names the layer, so this should no longer reach a publish. If it does, the publisher records it as a **skip** with the reason rather than retrying it on every future run. The only repair is re-exporting the animation without that mask, which changes the artwork -- an owner decision, never automatic. |
| CI red on push | A check failed (install/import/checks/dry-run). | `gh run view <id> --log-failed`; reproduce locally with `.\scripts\check.ps1`; the matrix is Python 3.11 **and** 3.12, so check which one failed. |
| `ModuleNotFoundError: No module named 'numpy'` from `coins\remap_ids.py` | numpy is the coin extra, not part of the core manifest. | `pip install -r requirements-coins.txt`. |
| `SuiteIsHermetic ... the hermetic guard was NOT active` | The suite was started without `-t .`, so `tests/__init__.py` never ran. | Use `.\scripts\check.ps1`, or the exact form the failure message prints. |

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
- **included flag** — per-item publish toggle set by the Curate emojikit.panel.
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
| `setStickerPositionInSet` | sync_order | Move a sticker; ids survive. |
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
| `collection/pack_plan.json` | no | The panel's move plan: which emoji should change pack, and which are parked. Written on every Save. |
| `coins/ticker_to_id.json` | no | Canonical ticker → custom_emoji_id map. The owner's own pack data, not part of the tool. |
| `coins/keywords.csv` | **yes** | ticker → name/keywords. |
| `coins/currency-emoji-inventory.md` | no | Inventory source. Names live custom-emoji ids, so it stays local. |
| `coins/currency-emoji-inventory.filled.md` | no | Generated, id-filled inventory. |
| `coins/rebuild_dedup_state.json` | no | Live coin pack set names/order. Written by `rebuild_dedup.py` **and** by the providers when they top the family up — they add their own `provider_in_flight` intent and `provider_added` tally beside the rebuild's keys, under the same pack-family lock. |
| `coins/rebuild_dedup_plan.json` | no | Frozen coin upload plan. |
| `coins/remap_live_cache.json` | no | remap signature cache. |
| `coins/ticker_to_id.<date>.bak.json` | no | Dated snapshot taken before a remap. **This is the revert target.** |
| `coins/unresolved_logos.json` | no | Tickers deliberately left unmapped because their source PNG is not their own logo. |
| `coins/ticker_to_id.prebroken.json` | no | Historic, and **not** a usable restore point despite the name — measured against the live stickers it scores the same as the map it was supposed to repair. |
| `logs/*.log` | no | Per-run UTC logs. |
| `state_<base>.json` | no | `build_pack` resume state. |

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
- The repository is **public** and licensed **GPL-3.0-or-later** (`LICENSE`).

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
5. **Blank emoji from gradient SVG** — the old svglib backend could not paint
   gradients and returned a fully transparent image. Fix: blank detection +
   raster fallback, then replacing the backend with resvg, which renders them.
   (§15.1)
6. **reportlab 3.12 wheel** — `reportlab<4` had no cp312 wheel, which pinned the
   whole project to Python 3.11. Fix: the reportlab/svglib stack was removed.
   (§3)
7. **Token in logs** — urllib3 DEBUG logged the bot-token URL. Fix: silence those
   loggers + `redact()`. (§26)
8. **Panel image 404** — `/img/<key>` wasn't URL-decoded. Fix: `unquote`. (§20)
9. **Resume duplicate** — positional resume could re-upload after a skip. Fix:
   dedup by the per-item committed `uploaded` flag + persisted `skipped`. (§6.4)
10. **Two publishers, one lock** — the pack-family lock decided ownership by
    reading a file, comparing a token and *unlinking* it, so its own stale-lock
    recovery could seat two processes at once; two real processes held one lock
    for 1.54 s. Fix: an OS-level lock (`msvcrt.locking` / `flock`) held for the
    whole critical section, and the lock file is never unlinked. (§12.5)
11. **A stranger's picture given our identity** — recovery accepted the single
    nearest candidate by perceptual hash, and dHash is a *grayscale structure*
    hash: an opaque red square and an opaque blue one are zero apart. Fix: a
    perceptual match may only NOMINATE; content verification decides, and
    "more than one" and "could not examine" stay distinct from "no match".
    (§13.6)
12. **Preflight claimed an acceptance it never got** — the counter counted
    attempts, so a run in which every check failed at the transport printed
    "all accepted" and exited 0. Fix: four counters, and an unreachable
    Telegram is its own outcome. (§12.5)
13. **Alpha applied twice** — `fit_100` pasted the image using itself as the
    mask, which composites against the transparent canvas beneath: colour came
    back multiplied by alpha and alpha squared. `(255,0,0,128)` fitted to
    `(128,0,0,64)`. Fix: an unmasked copy onto a fully transparent
    destination. (§14.1)
14. **Video identity ignored alpha, and disagreed with itself** — the default
    `vp9` decoder drops the alpha layer in silence, so two clips differing only
    in opacity shared one content key; and the two fingerprint APIs sampled
    differently, so one clip had two hashes. Fix: one `video_decode` module
    that picks the decoder from the probed codec. Existing rows are migrated
    deliberately by `scripts/identity_repair.py`, never implicitly. (§14.2,
    §13.6)
15. **A stale tab spoke for the whole catalog** — `/api/save` carries the full
    selection, so a page opened before another change silently re-included rows
    it had never seen, and answered `{"ok": true}`. Fix: the request states its
    scope; a page too old to say gets 409. (§20)
16. **A Lottie timeline that was not a number** — `fr = NaN` passed
    `validate_tgs`, because every comparison against NaN is False. Fix: an
    explicit `math.isfinite` check on `fr`/`ip`/`op`. (§14.3)
17. **Video identity degraded open, and cached the degradation** — a missing
    `libvpx-vp9`, a failed codec probe and a container naming no codec all
    answered "no decoder needed", which drops VP9's separate alpha layer; one
    transient probe error was then cached, so every later decode of that file
    in the process lost its alpha silently. Two clips differing only in opacity
    share one key, and `Catalog.add` merges them and deletes the file it merged
    away. Fix: `UndecodableVideo` — establish fidelity or refuse. (§14.2)
18. **A video was compared at one frame** — two clips sharing ten opening frames
    compared equal, and so did two differing in exactly one frame. Fix: walk the
    timeline at 30 fps, and stop treating content-key equality as proof for
    video. (§14.3)
19. **A migration moved three tables and stopped** — the owner's state file was
    left naming 51 keys the catalog no longer had, which `reconcile_set` reads
    as a reordered pack; the derived hashes stayed stale; the archived filenames
    kept the old key. Fix: one versioned migration over every durable reference,
    with a journal. (§13.6)
20. **The migration backup was not WAL-safe** — `copy2` of the `.db` alone
    opened as a database with `no such table: items`. Fix: SQLite's online
    backup API, verified before anything is changed. (§13.6)
21. **One verified candidate beat an unreadable rival** — with two candidates
    matching, deleting one candidate's FILE made the resolver answer "unique"
    with the survivor. Removing evidence must not promote a guess. (§13.6)
22. **The panel confused "a save is in flight" with "there is unsaved work"** —
    an acknowledgement of an older snapshot cleared the dirty state, said
    "Saved" and let the tab close on newer ticks. (§20)
23. **A refusal retrying could not fix was retried forever** — and the
    five-second heartbeat walked past the backoff, while a fetch that never
    resolved claimed the queue for the life of the page. (§20)
24. **A Worker error could echo a bot token** — every Bot API URL embeds it and
    a transport failure names the URL, which reached the HTTP 502 body and every
    log sink. (§12.9)

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
.venv\Scripts\python.exe -m pip install -r requirements.txt         # core
.venv\Scripts\python.exe -m pip install -r requirements-coins.txt   # only for coins/remap_ids.py
copy .env.example .env            # then edit tokens + owner id
.\run.ps1                         # launcher menu
.\run.ps1 -Check                  # env doctor (CI-style)
.\scripts\check.ps1               # byte-compile + full unit suite

# --- single pack (general) ---
$PY -m emojikit.make_emoji_pngs --in input\set --out build\set
$PY -m emojikit.build_pack --base set --title "Set" --source-dir build\set --token-env GENERAL_BOT_TOKEN --dry-run
$PY -m emojikit.build_pack --base set --title "Set" --source-dir build\set --token-env GENERAL_BOT_TOKEN

# --- collector ---
$PY -m emojikit.fetch_pack <pack-or-link> --token-env GENERAL_BOT_TOKEN
$PY -m emojikit.fetch_emoji_ids --ids-file <file-with-premium-ids> [--id <n>]
$PY -m emojikit.add_media --in input\set --emoji 😀
$PY -m emojikit.panel                                            # curate, then Save
$PY -m emojikit.build_collection --base mypack --title "My Pack" --token-env GENERAL_BOT_TOKEN --dry-run
$PY -m emojikit.build_collection --base mypack --title "My Pack" --token-env GENERAL_BOT_TOKEN
$PY -m emojikit.sync_order --base mypack                          # reorder a LIVE pack
$PY -m emojikit.sync_order --base mypack --apply

# --- bot ---
$PY -m emojikit.emoji_bot

# --- coins ---
$PY coins\fetch_logos.py
$PY -m emojikit.make_emoji_pngs --in coins\logos\svg --out coins\logos\emoji
$PY -m emojikit.make_emoji_pngs --in coins\logos\png --out coins\logos\emoji
$PY coins\rebuild_dedup.py
$PY coins\remap_ids.py --emoji-dir "<coin logo folder>" --max-distance 200 --apply
$PY coins\enhance_map.py
$PY coins\alias_map.py
$PY coins\check_all_packs.py
$PY coins\verify_logos.py --emoji-dir "<coin logo folder>"
$PY coins\verify_logos.py --emoji-dir "<coin logo folder>" --fix --only sol,xrp
$PY coins\write_manifests.py --out-dir "<coin archive folder>"

# --- tests / CI-locally ---
.\scripts\check.ps1                                     # what CI runs (compile + suite)
$PY -m unittest discover -s tests -t . -p "test_*.py"   # -t . is REQUIRED (see §10)
$PY -m compileall -q .
$PY -c "from emojikit import build_pack, make_emoji_pngs, fetch_pack, fetch_emoji_ids, add_media, build_collection, emoji_bot, panel"

# --- git ---
git add -A; git commit -m "..."; git push origin main
gh run list --repo KiaroSama/Emoji-Mapper --limit 1 --json status,conclusion
```

---

## Appendix B — Example session transcripts (illustrative)

### B.1 Fetch + curate + publish

```
> python -m emojikit.fetch_pack https://t.me/addemoji/RMaccs --token-env GENERAL_BOT_TOKEN
[..] Authenticated bot: @YourEmojiBot
[..] Pack RMaccs (Accounts store — @RMaccs): 80 stickers -> new=80 dedup=0 failed=0
Done. new=80 dedup=0 failed=0
  catalog static: 80 total (80 pending upload)

> python -m emojikit.panel
Emoji curate panel: http://127.0.0.1:9450/
# (open browser, untick a few, click Save -> "Saved ✓  76 included · 4 excluded")

> python -m emojikit.build_collection --base accts --title "Accounts" --token-env GENERAL_BOT_TOKEN --dry-run
DRY RUN: nothing uploaded.
  static: 76 emoji -> 1 set(s) named acctss1_by_<bot> ...
  video: 0 emoji -> 0 set(s) ...
  animated: 0 emoji -> 0 set(s) ...
```

### B.2 Resolve an id and download its pack

```
> python -c "...getCustomEmojiStickers(['5283254221590787816'])..."
set_name: RMaccs | format: static
> python -m emojikit.fetch_pack RMaccs --token-env GENERAL_BOT_TOKEN
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
  docs in `docs/`; shipped images in `assets/`; the Cloudflare Worker in
  `worker/`. Don't clutter the root.
- **No new dedup/mapping mechanisms** — extend the catalog (§5, §13, §16).
- **Never** commit `.env`, `secrets.md`, `collection/`, `logs/`, tokens.
- **Logging** is mandatory for executable scripts via `emojikit.logsetup`
  (UTC file logs; secrets redacted).
- **Verify before claiming done:** `.\scripts\check.ps1` + a real run; for UI, a
  real browser (Playwright) at the relevant breakpoints.
- **Sync:** push to `main` and keep this guide + `README.md` current.

*This guide is the single source of truth for how Numera Emoji Mapper works. If code
and guide disagree, fix whichever is wrong and re-sync.*

---

## Appendix D — Building the next pack: the fast path

Distilled from the first full 200-emoji pack. The steps are the short version of
§6; the traps below each cost real time, and every one of them is now either
prevented by code or answerable in seconds if you know to look.

### The order that works

```powershell
$PY -m emojikit.fetch_emoji_ids --ids-file ids.txt      # or emojikit/fetch_pack.py / emojikit/add_media.py
$PY -m emojikit.panel                                   # curate + order, then Save selection
# check the header: it warns when the total exceeds one pack
$PY -m emojikit.build_collection --base <Base> --title "<Title>" --mixed --dry-run
$PY -m emojikit.build_collection --base <Base> --title "<Title>" --mixed
$PY -m emojikit.sync_order --base <Base>                # report; --apply to place them
```

### Count the brand logo

It is the first emoji of every set and occupies one of Telegram's 200. **200
catalog items + the logo = 201, which publishes as TWO sets.** For a single
pack, include 199. The panel numbers the logo #1 and warns in the header, so
trust the header, not your own count of the grid.

### Preflight runs automatically

Launcher **B3** now runs `--preflight` between the dry run and the upload: every
queued file is offered to Telegram's validator first, and a refusal stops the
run before a single sticker is published. About a minute for a 200-emoji queue,
against the 46 it cost to discover the same file mid-publish. Run it by hand
with `$PY -m emojikit.build_collection --base <Base> --title "<T>" --preflight`.

### Probe a suspect file before a 45-minute run

`uploadStickerFile` runs the **same validator** as `addStickerToSet` and touches
no pack, so it answers "will Telegram take this file?" in one call. Worth doing
for anything unusual before starting a long publish — a single refused emoji
cost a whole run here.

**A `.tgs` Telegram happily *plays* can still be refused on *upload*.** The
uploader's Lottie validator is stricter than the player: a **subtract mask**
(`mode: "s"`) is rejected, while add masks pass. Proven by sending Telegram's
own untouched original back and having it refused. Our re-encode is innocent.

The way out is to stop asking the Lottie validator at all: **render the
animation and ship it as a VIDEO emoji.** The mask is applied by the renderer,
so the artwork is unchanged, and `format=video` never goes near the `.tgs` path.

**And the mirror case: a video source can UNDER-DECLARE its own length.** One
sticker served by Telegram carried a `duration=3.000` container header over
3.916 s of packets — 94 frames ending at 3.875 s. Telegram's uploader reads the
header, so the original was accepted; our `-c copy` remux recomputes duration
from the packets, wrote the truthful 3.916 s, and came back
`STICKER_VIDEO_LONG`. The picture was identical, frame for frame. Here our
re-encode is the honest one and the source is not.

`reencode_in_place` therefore keeps the original whenever a remux would cross
`WEBM_MAX_SECONDS`, exactly as it already does for the byte caps: a byte-clone
is a lesser failure than an upload that cannot happen. To publish such a source
as our own bytes, re-time it first — speeding the whole loop to fit keeps the
animation, where trimming the tail does not.

### Expect flood waits, and read the log

A 200-emoji publish takes roughly 45 minutes, most of it in Telegram's flood
waits (240–270 s each). The log records every wait and every successful upload
(`uploaded <key> -> <set> #<n> (n/total this run)`), so a quiet log now means
stopped, not slow.

### Stopping is safe; resuming is automatic

Every upload commits its own flag, so a killed run loses nothing. The lock its
process left behind frees itself **120 seconds** after the process dies — just
re-run the same command. `reconcile_set` attributes anything that landed without
being recorded.

### The channel link waits for a clean run

A trailing pack is announced only by a run with **no failure and no skip**. If
one is withheld, the log says so, and the next clean run posts it.

### Order is not frozen at publish time

`emojikit/sync_order.py` moves stickers with `setStickerPositionInSet`: no re-upload, and
`file_id` **and** `custom_emoji_id` survive, so nobody using the emoji is
affected. Run it report-only first; it refuses a set holding any sticker the
catalog cannot identify.

### Two things that will mislead a measurement

- **Telegram re-encodes what it stores.** Never confirm an upload by exact
  content key — the key is a SHA of exact pixels, so a lossy re-encode changes
  it for a picture that is visually identical. `media.same_image` owns this
  comparison for every caller: exact key first, then **both** a dHash and a
  colour check, since neither alone is safe. dHash survives the re-encode but
  is grayscale, so a stranger's green square sits 4 bits from our red one; the
  mean channel delta sees colour but not structure. Measured over 30 known-same
  and 30 known-different pairs across three live packs — dHash 0–3 vs 12–47
  bits, mean delta 0.02–2.35 vs 35.46–188.22.
  A false negative costs a halted publish and is recoverable; a false positive
  attributes a stranger's sticker to our item and is not. That asymmetry is why
  it asks for two independent agreements, and why anything it cannot compare
  (animated is vector, so there is no raster hash) returns *undecidable* rather
  than *no*.
- **VP9 keeps alpha in a separate layer.** Probing a video emoji without
  `-c:v libvpx-vp9` reports every one of them as opaque, correct ones included.
  The same flag is required when re-encoding, or transparency is silently lost.

### Keeping a copy

The published files are already on disk under `collection/media/<format>/`,
named by content key, with `collection/manifests/<set>.md` listing what went
where. There is no export command; ask for one if you want the pack zipped in
pack order with an index.
