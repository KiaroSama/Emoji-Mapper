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
  telegram_api.py          the Bot API client, its errors and Telegram's caps
  packstate.py             state-file shape + atomic write + pack-family lock
  announce.py              announce finished packs (Worker, or direct)
  make_emoji_pngs.py       image -> 100x100 PNG (static)
  fetch_pack.py            collector: download a Telegram pack -> catalog
  fetch_emoji_ids.py       collector: download specific emoji by ID -> catalog
  add_media.py             collector: build emoji from local files -> catalog
  build_collection.py      collector: publish the catalog into new packs
  collection_state.py      publisher plan/resume state + brand logo
  collection_reconcile.py  what is live in a set, and whose key each sticker is
  sync_order.py            reorder an already published pack (no re-upload)
  panel.py                 web "Curate" panel: pick which emoji to publish
  emoji_bot.py             interactive bot: extract premium-emoji IDs (tap-to-copy)
  emojikit/                shared core library
    logsetup.py            UTC file logging (logs/)
    media.py               format detect + conversions (static/video/tgs)
    identity.py            content keys, perceptual hashes, same_image
    catalog.py             content-addressed SQLite catalog (dedup + inclusion)
  coins/                   the crypto-coin component (see §7)
    _paprika_api.py        CoinPaprika HTTP + candidate search + logo decode
  scripts/check.ps1        byte-compile + full unit suite (CI runs this too)
  tests/                   unit tests + fixtures (see tests/README.md and §10)
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
# Core manifest: requests + Pillow + resvg-py + rlottie-python. Everything
# except one coin tool
# runs on this alone.
.venv\Scripts\python.exe -m pip install -r requirements.txt
# Coin extra: numpy, imported only by coins/remap_ids.py (~20 MB wheel + BLAS,
# so it is not in the core set). Skip it unless you work on coins/.
.venv\Scripts\python.exe -m pip install -r requirements-coins.txt

# 2. Configure secrets
copy .env.example .env
# edit .env and fill in tokens + owner id (see keys below)
```

`.env` keys:

```
PACK_OWNER_USER_ID=<your numeric Telegram id>   # owns every created set; press Start on each bot once
TELEGRAM_BOT_TOKEN=<coin bot token>
GENERAL_BOT_TOKEN=<general bot token>
BOT_ALLOWED_USER_IDS=<optional: extra ids allowed to use emoji_bot.py>
PACK_LINKS_CHAT_ID=<optional: channel that receives finished-pack links>
WORKER_PUBLISH_URL=<optional: https://<worker>.workers.dev/publish — see §12.9>
WORKER_PUBLISH_SECRET=<optional: bearer for that endpoint; both or neither>
EMOJI_LOG_RETENTION_DAYS=30    # optional: prune logs/ older than N days (0 = keep all)
EMOJI_FFMPEG_TIMEOUT=300       # optional: seconds per ffmpeg/ffprobe child
CMC_API_KEY=<optional CoinMarketCap key, only for coins/fetch_cmc.py>
```

That is the complete set of variables any code reads, plus two environment-only
switches used for testing: `TELEGRAM_API_BASE` (Bot API endpoint override,
default `https://api.telegram.org`) and `EMOJI_MAPPER_NO_DOTENV=1` (makes
`build_pack.load_env()` a no-op; it is read *before* `.env`, so it only works
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
  B4) Open web panel to pick & reorder emoji (browser)

Bot
  C1) Run the Emoji Mapper bot (premium-emoji ID extractor)

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
.venv\Scripts\python.exe panel.py [--data-dir collection] [--port 9450] [--preview-fps 15] [--no-open]
```

Dark neon panel: every emoji is a big labelled card (static=image,
video=`<video>`, animated `.tgs`=pre-rendered to animated WebP). All selected by
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
(`collection/manifests/<set>.md`: name + emoji ID) is written. The manifest
counts the **pack**, not the catalog rows: when a brand logo leads the set it is
row 1 and the catalog items follow from 2, because the logo is a sticker in the
pack even though it is not a catalog item.

---

## 7. Crypto-coin component (`coins/`)

Self-contained tool that reuses `build_pack.py` and the coin bot.

```powershell
# Logos (data): fetch + keywords
.venv\Scripts\python.exe coins\fetch_logos.py          # CoinGecko logos + keywords.csv
.venv\Scripts\python.exe coins\fetch_paprika.py --dry  # resolve + report only, nothing published
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

## 8. The Emoji Mapper bot (`emoji_bot.py`)

Long-polling bot (run it and leave it running; only one instance at a time):

```powershell
.venv\Scripts\python.exe emoji_bot.py    # uses GENERAL_BOT_TOKEN; or run.ps1 -> C1
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
.venv\Scripts\python.exe -m pip install ruff                       # once: linter, not a runtime dep
.\scripts\check.ps1                                                # compile + lint + full suite
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"   # the suite alone
.\run.ps1 -Check                                                   # env doctor (venv/deps/ffmpeg/.env)
```

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
   `build_collection.reconcile_set` attribute anything ambiguous from the live
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
| `--per-set` | `200` | Emojis per set. Accepted range is **1–200** (Telegram's cap); anything larger exits 2 with a usage error. |
| `--limit` / `--start` | `0` / `0` | Process a slice of the source. |
| `--state` | `state_<base>.json` | Resume file (per pack, never clobbered). |
| `--dry-run` | off | Validate inputs without calling Telegram. |
| `--preflight` | off | Ask Telegram to validate every queued file, then stop. Publishes nothing; non-zero exit if any file is refused. |
| `--repaint` | off | Create NEW sets with `needs_repainting`, so the client paints every emoji in them the text/accent colour. Whole-set and creation-only: it cannot be added later and it flattens colour art. |

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
| `--repaintable` | `ask` | What to do with emoji Telegram REPAINTS (`ask`/`skip`/`keep`). The client overrides their colours, so the source pack does not show the stored art: in one of ours they render that art instead — sometimes flat black, sometimes full colour. Look before you decide; with no terminal to answer, `ask` skips them. |

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


**Set titles are one sequence across every format.** `--title "@GodVerify Emoji
Packs"` produces `@GodVerify Emoji Packs 1`, `2`, `3` … in creation order,
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
| `--brand-logo` | `assets/god-verify-emoji-logo.png` | First-emoji brand logo (Emoji Mapper bot only). |
| `--no-brand-logo` | off | Disable the mandatory first-emoji logo. |
| `--dry-run` | off | Show the plan without uploading. |

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

**Brand logo (first emoji of every set).** When publishing with the
`@GodVerifyEmojiMapperbot` bot, the God Verify logo is inserted as the **first
emoji of every set** (`--brand-logo`, default `BRAND_LOGO_DEFAULT` in
`build_collection.py` = the repo's own `assets/god-verify-emoji-logo.png`, so a
fresh clone works with no machine-specific path). Since Bot API 7.2
(March 2024) a single custom-emoji set may contain **mixed formats**, so the
logo is always a **static** 100x100 PNG and leads a static, video *or* animated
set alike (verified live). The `@GodVerifyCoinEmojiMapperbot` coin bot is exempt.
Disable with `--no-brand-logo`. The logo occupies position 0, so item
`custom_emoji_id`s are read from position 1 onward (handled automatically).

### 12.5b `sync_order.py` — reorder an ALREADY PUBLISHED pack

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
$PY sync_order.py --base mypack             # what would move
$PY sync_order.py --base mypack --apply
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

### 12.5c `pack_manifest.py` — the roster of what is in every published pack

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

**It gates its media like the panel.** Every animated card inlines a still as
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

### 12.5d `pack_archive.py` — the finished pack's media leaves the project

Once a pack is FULL its media moves out to the owner's archive, one folder per
pack, and the project keeps no copy. A pack still being filled is left alone on
purpose: the filename carries the emoji's **slot**, and an unfinished pack can
still be reordered, which would make every name in its folder wrong.

| Flag | Default | Meaning |
|------|---------|---------|
| `--check` | — | Does the archive still describe the packs? Exit 3 if not. Local only, no network. |
| `--sync` | — | Archive every FULL pack and regenerate its metadata from the live set. |

`EMOJI_ARCHIVE_DIR` overrides the archive root (default
`F:\Stickers and Emojis\Emojis`). Each folder holds:

```
001_logo_god-verify.png              the brand logo, copied from assets/ (never moved)
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

### 12.6 `panel.py` — curate web panel

| Flag | Default | Meaning |
|------|---------|---------|
| `--data-dir` | `collection` | Catalog/media directory. |
| `--all` | off | Also show emoji already live in a pack. |
| `--with-pack N` | off | Also show the emoji already live in pack **N**. Repeatable. |
| `--port` | `9450` | Local port. |
| `--preview-fps` | `15` | Frame rate for animated previews. The grid decodes every frame of every visible card, so this is the main lever on how heavy the panel feels. Lower it if it drags. |
| `--no-open` | off | Don't auto-open the browser. |

Interactions: **click** a card to toggle include/exclude, **click the
`premium-id:` label** to copy that id to the clipboard (it stops there and does
not toggle the card), **drag** a card to reorder (this is the publish order).
Each card shows its **publish position** at the top; the numbers are recomputed
from the order on every drop, never stored on the card. **The brand logo is
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
  added by `fetch_emoji_ids.py` afterwards was invisible until a restart. The
  view is now re-read from the database on every page load (200 rows, a few
  milliseconds), replacing the shared list **in place** — rebinding it would
  leave every route closed over the old object;
* `socketserver` sets `SO_REUSEADDR` by default, and **on Windows that lets a
  second bind succeed on a port that already has a live listener**. Two panels
  then ran, both logging `Panel at …`, the browser reached whichever socket the
  OS picked, and the older process kept serving its own start-up snapshot —
  which is why only closing the launcher (killing every instance) made a change
  appear. `allow_reuse_address` is now off, so a second panel exits 2 with what
  to do about it instead of quietly sharing the port.

Editing `panel.py` itself still needs the process restarted — a refresh asks the
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
`panel.DEFAULT_PORT`, never typed again), and deletes the clone
on exit:

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

**Performance.** Animated `.tgs` are pre-rendered server-side to an **animated
WebP** (`media.lottie_preview_webp`, rlottie) and served as a plain
`<img loading=lazy>`, so the browser animates them on the compositor. There is
no Lottie player and no animation library in the page. This replaced a
lottie.js SVG player per card, which cost ~704 DOM nodes each -- measured on a
146-animation catalog, the document went from 1 426 nodes with none mounted to
8 476 with ten, and every scroll rebuilt a row's worth. Previews are cached
under `<data-dir>/preview/` (~9 MB for 146 at 15fps/q60), keyed by content hash
and frame rate, so they are built once. The frame rate is the lever that decides
how heavy the grid feels, because the browser decodes every frame of every
animated card: measured per animation, 30fps costs 54 frames / 119 KB against
15fps's 28 / 60 KB. Use `--preview-fps` to go lower.

**Only cards near the viewport carry the animated frames.** Each animated
preview is rendered twice -- `?still=1` (frame 0, ~3 KB) and the animated file
(~60 KB) -- and one `IntersectionObserver` (300-px margin) swaps `img.src`
between them. `content-visibility:auto` alone was not enough: a decoded
off-screen animation still costs its full frame buffer, so the swap is what
bounds the work to what is on screen. **Animation: On** in the header (default
on, persisted in `localStorage`) forces every card back to the still. Benign
browser disconnects while scrolling are swallowed server-side (no
`ConnectionAbortedError` traceback spam).

**Brand logo preview.** If `GENERAL_BOT_TOKEN` resolves to
`@GodVerifyEmojiMapperbot` and the logo file (`BRAND_LOGO_DEFAULT` in
`build_collection.py`) exists, the panel shows it as a distinct **gold-bordered
first card** labelled "Brand logo (auto-added on publish)" so you can see where
it will land *before* publishing. This card is preview-only: it's not clickable,
not counted in the included/excluded totals, and never sent to `/api/save` — the
logo itself is never part of the catalog and is only actually inserted by
`build_collection.py` at publish time (see §12.5). For the coin bot, or if the
logo file is missing, the card is simply not shown.

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
it — `panel.py --with-pack 5` shows pack 5's emoji and the unpublished ones
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

### 12.7 `emoji_bot.py` — premium-emoji ID extractor bot

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
stops `emoji_bot.py` (§12.7, §19) receiving anything on that token;
`deleteWebhook` hands it back. Run one or the other per token.

`ADMIN_USER_IDS` **fails closed**, exactly like `emoji_bot.allowed_user_ids()`:
unset, empty or all-invalid means the bots answer nobody. Only plain positive
integers count — `Number()` would have accepted `0x10`, `12.5` and `1e3`.
A stranger gets one reply in private and **silence in a group**, so the bot
cannot be turned into a spam vector.

Webhook handlers return **200 even when handling fails**. Telegram redelivers
any non-2xx and every action here is a `sendMessage`, so a redelivery after a
partial success posts the reply twice; failures are logged instead. Nothing is
retried internally, for the same reason as the Python client (§5).

Local side: set `WORKER_PUBLISH_URL` **and** `WORKER_PUBLISH_SECRET` and
`build_collection.notify()` routes links through the Worker; leave either unset
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
`build_pack.load_env` and pipes each value to `wrangler secret put` **through
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

A **`.webm` input is decoded with `-c:v libvpx-vp9`**, named explicitly before
`-i`. VP9 keeps alpha in a separate WebM layer that ffmpeg's default vp9 decoder
drops without a word, so the filter chain would see no alpha and the transparent
pad would land on an opaque frame — a re-encoded transparent emoji came out a
black square. GIF/PNG inputs are unaffected and get no decoder override. The
same flag is needed to *inspect* one: probing a VP9 emoji with the default
decoder reports every one of them as opaque, correct ones included.

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
.venv\Scripts\python.exe coins\remap_ids.py --emoji-dir "F:\...\emoji" --max-distance 200 --apply
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
#    (browser opens http://127.0.0.1:9450/ ; click cards / Shift+click ranges / Save)

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

## 20. The Curate panel internals (`panel.py`)

A `ThreadingHTTPServer` on `127.0.0.1`. Routes:

| Route | Response |
|-------|----------|
| `GET /` | The single-page HTML (items embedded as JSON). |
| `GET /img/<key>` | The media bytes (webp/png/webm) with correct MIME. |
| `GET /preview/<key>?fps=N` | A `.tgs` rendered to an **animated WebP**, cached on disk. The rate is in the URL because the response is immutable-cached. |
| `GET /static/<file>` | Static assets (logo, favicon), traversal-guarded. |
| `POST /api/save` | Body `{"excluded":[keys]}` → `catalog.set_inclusion(...)`. |
| `POST /api/order` | Body `{"order":[keys]}` → `catalog.set_order(...)` (drag-to-reorder = publish order). |

Both POST routes are mutation endpoints and are guarded: loopback-only `Host`/
`Origin`, a per-run token sent as `X-Panel-Token`, an exact-permutation check on
the order, a content-type check and a body cap (`tests/test_panel.py`).

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
  Header buttons: ↑ Top / ↓ Bottom / Select all / Deselect all / Invert /
  Backdrop / Animation: On|Off / Save. The two jump buttons scroll the
  **document**, not `scrollIntoView`: that aligns an element with the top of
  the viewport, which sits behind the sticky header, so Top stopped a
  header-height short — hiding the Pack 1 marker — and Bottom stopped short for
  the same reason.
- **Pack boundaries are drawn in the grid.** When the selection needs more than
  one set, a full-width marker carrying the brand logo sits at the head of each
  pack, labelled with the grid range it spans (`Pack 2 · #201–#399`), so you can
  see where each published pack will start and end while still curating.
  Two things it deliberately does: the splits are counted from **included**
  items only, since an unticked card never reaches Telegram and so cannot push
  the next emoji into the following pack — which is why the markers move as you
  tick and untick, not only when you drag. And a marker is a `.packsep`, never
  a `.card`: the drop handler resolves its target with `closest('.card')`, so a
  marker that matched would swallow a drop aimed past it and silently do
  nothing. Card numbers stay **grid** positions and are unchanged by the
  markers.
- **Static and animated are both plain `<img loading="lazy">`** — the browser
  owns decoding and compositing, and there are no player objects to build or
  tear down. Video is `<video preload="metadata">` (muted, looping,
  `playsinline`), started by the observer rather than by the `autoplay`
  attribute, so playback is bounded the same way everything else is.
- **One `IntersectionObserver` (300-px margin) drives all of it**: it swaps an
  animated card's `src` between the still (`?still=1`) and the animated WebP,
  and plays/pauses `<video>`. A grid of *everything* playing was the original
  CPU sink; the fix is the viewport bound, not hover — a grid of frozen stills
  cannot be curated, which is why hover-only was rejected for both.
  `content-visibility:auto` is set as well but does not by itself stop an
  off-screen animation from costing its decoded buffers.
- `prefers-reduced-motion` is respected: nothing plays by itself, and hover
  becomes the only way to play a video, which is what those handlers are for.

The panel ships no animation library: animated emoji are rasterised to WebP by
`rlottie-python` on the server, so the page needs nothing from a CDN and works
offline.

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
| Bot stops with **409 Conflict** | Two `getUpdates` consumers. | Run only one `emoji_bot.py` instance. |
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
| `coins/ticker_to_id.json` | **yes** | Canonical ticker → custom_emoji_id map. |
| `coins/keywords.csv` | **yes** | ticker → name/keywords. |
| `coins/currency-emoji-inventory.md` | **yes** | Inventory source. |
| `coins/currency-emoji-inventory.filled.md` | no | Generated, id-filled inventory. |
| `coins/rebuild_dedup_state.json` | **yes** | Live coin pack set names/order. Written by `rebuild_dedup.py` **and** by the providers when they top the family up — they add their own `provider_in_flight` intent and `provider_added` tally beside the rebuild's keys, under the same pack-family lock. |
| `coins/rebuild_dedup_plan.json` | no | Frozen coin upload plan. |
| `coins/remap_live_cache.json` | no | remap signature cache. |
| `coins/ticker_to_id.<date>.bak.json` | **yes** | Dated snapshot taken before a remap. **This is the revert target.** |
| `coins/unresolved_logos.json` | **yes** | Tickers deliberately left unmapped because their source PNG is not their own logo. |
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
$PY sync_order.py --base mypack                          # reorder a LIVE pack
$PY sync_order.py --base mypack --apply

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
.\scripts\check.ps1                                     # what CI runs (compile + suite)
$PY -m unittest discover -s tests -t . -p "test_*.py"   # -t . is REQUIRED (see §10)
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
Emoji curate panel: http://127.0.0.1:9450/
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
  docs in `docs/`; shipped images in `assets/`; the Cloudflare Worker in
  `worker/`. Don't clutter the root.
- **No new dedup/mapping mechanisms** — extend the catalog (§5, §13, §16).
- **Never** commit `.env`, `secrets.md`, `collection/`, `logs/`, tokens.
- **Logging** is mandatory for executable scripts via `emojikit.logsetup`
  (UTC file logs; secrets redacted).
- **Verify before claiming done:** `.\scripts\check.ps1` + a real run; for UI, a
  real browser (Playwright) at the relevant breakpoints.
- **Sync:** push to `main` and keep this guide + `README.md` current.

*This guide is the single source of truth for how Emoji Mapper works. If code
and guide disagree, fix whichever is wrong and re-sync.*

---

## Appendix D — Building the next pack: the fast path

Distilled from the first full 200-emoji pack. The steps are the short version of
§6; the traps below each cost real time, and every one of them is now either
prevented by code or answerable in seconds if you know to look.

### The order that works

```powershell
$PY fetch_emoji_ids.py --ids-file ids.txt      # or fetch_pack.py / add_media.py
$PY panel.py                                   # curate + order, then Save selection
# check the header: it warns when the total exceeds one pack
$PY build_collection.py --base <Base> --title "<Title>" --mixed --dry-run
$PY build_collection.py --base <Base> --title "<Title>" --mixed
$PY sync_order.py --base <Base>                # report; --apply to place them
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
with `$PY build_collection.py --base <Base> --title "<T>" --preflight`.

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

`sync_order.py` moves stickers with `setStickerPositionInSet`: no re-upload, and
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
