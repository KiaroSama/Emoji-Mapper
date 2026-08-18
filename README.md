<div align="center">

<img src="assets/emoji-mapper-logo.png" alt="Emoji Mapper" width="180">

# Emoji Mapper

**Build Telegram premium custom-emoji packs from any collection of images.**

</div>

Emoji Mapper started as a crypto-coin logo packer and is now a general tool: it
takes a folder of images, normalizes each one to the exact 100×100 PNG that
Telegram custom emoji require, and uploads them through a bot into one or more
custom-emoji sets owned by your account.

It ships with two independent workflows that share the same engine:

| | Workflow | Bot | Source | Purpose |
|---|----------|-----|--------|---------|
| <img src="assets/coin-emoji-mapper-logo.png" alt="" width="42"> | **Crypto coins** | `TELEGRAM_BOT_TOKEN` | CoinGecko / CoinPaprika / CoinMarketCap logos | the original coin-logo packs |
| <img src="assets/emoji-mapper-logo.png" alt="" width="42"> | **General** | `GENERAL_BOT_TOKEN` (`@GodVerifyEmojiMapperbot`) | any folder of images you provide | any non-coin emoji pack |

The same scripts (`make_emoji_pngs.py` + `build_pack.py`) power both; only the
source folder and the selected bot token differ.

> 📘 **[Full Guide (0 → 100) — docs/GUIDE.md](docs/GUIDE.md)** — complete
> reference for every workflow and command, written for humans and AI agents.

## How it works

1. **Prepare PNGs** — `make_emoji_pngs.py` converts your images (SVG, PNG, JPG,
   WEBP, GIF) into transparent 100×100 PNGs.
2. **Build the pack** — `build_pack.py` uploads those PNGs into Telegram
   custom-emoji sets (max 200 per set), naming them `<base><n>_by_<botusername>`
   and sending you each finished pack's `https://t.me/addemoji/...` link.

## Requirements

- Python 3.11+
- A Telegram bot (create one with [@BotFather](https://t.me/BotFather))
- Your numeric Telegram user id (the pack owner) — press **Start** on the bot once
- Dependencies in `requirements.txt` (`pip install -r requirements.txt`) — this
  is the **core** set (requests, Pillow, resvg-py, rlottie-python) and is all the general
  workflow needs. The coin tool `coins/remap_ids.py` additionally needs numpy,
  which lives in `requirements-coins.txt` (a ~20 MB wheel nobody building
  ordinary packs has to install)
- **ffmpeg + ffprobe** on `PATH` — only required for **video** emoji (`.webm`).
  Install on Windows with `winget install Gyan.FFmpeg`. Static and animated
  workflows do not need it.

## Setup

```powershell
# 1. Create a virtual environment and install deps
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
# working on the crypto-coin component too? add the coin extra:
# .venv\Scripts\python.exe -m pip install -r requirements-coins.txt

# 2. Configure secrets
copy .env.example .env
# then edit .env and fill in the tokens / owner id
```

`.env` keys (see `.env.example`):

```
PACK_OWNER_USER_ID=<your numeric telegram id>
TELEGRAM_BOT_TOKEN=<coin bot token>
GENERAL_BOT_TOKEN=<general bot token>      # @GodVerifyEmojiMapperbot
CMC_API_KEY=<optional CoinMarketCap key>
```

## Quick start — general emoji pack (new bot)

```powershell
# Put your source images in a folder, e.g. input/myset/
.venv\Scripts\python.exe make_emoji_pngs.py --in input\myset --out build\myset

# Validate without calling Telegram
.venv\Scripts\python.exe build_pack.py --base myset --title "My Emojis" `
    --source-dir build\myset --token-env GENERAL_BOT_TOKEN --emoji "😀" --dry-run

# Build for real
.venv\Scripts\python.exe build_pack.py --base myset --title "My Emojis" `
    --source-dir build\myset --token-env GENERAL_BOT_TOKEN --emoji "😀"
```

Optional: pass `--keywords path\to\keywords.csv` (columns `ticker,...,keywords`)
to attach searchable keywords to each emoji. Without it, the file name is used.

## `build_pack.py` options

| Option | Default | Description |
|--------|---------|-------------|
| `--base` | *(required)* | set-name base (letters/digits/_) |
| `--title` | *(required)* | human-readable set title |
| `--source-dir` | `logos/emoji` | folder of 100×100 PNGs to upload |
| `--token-env` | `TELEGRAM_BOT_TOKEN` | env var holding the bot token |
| `--keywords` | `auto` | keywords CSV; `auto` = coin list only for the default source |
| `--emoji` | 🪙 | associated standard emoji |
| `--user-id` | `PACK_OWNER_USER_ID` | numeric owner id |
| `--per-set` | 200 | emojis per set; 1–200 only (Telegram's cap), higher is a usage error |
| `--limit` / `--start` | 0 / 0 | process a slice of the source |
| `--state` | `state_<base>.json` | resume file (per pack, never clobbered) |
| `--dry-run` | off | validate inputs without calling Telegram |

Runs are **resumable**: progress is saved to `state_<base>.json`, so an
interrupted or flood-limited run continues without recreating existing sets.

## Collecting & republishing packs (multi-format)

The collector workflow downloads emoji from existing Telegram packs (and/or
builds them from scratch from your own files), deduplicates everything into a
persistent catalog, and republishes them into new packs with your own name and
labels. It supports all three custom-emoji formats — **static** (PNG/WEBP),
**animated** (`.tgs` Lottie) and **video** (`.webm` VP9).

### Animated vs. video — and how detection works

| | Animated (`.tgs`) | Video (`.webm`) |
|---|---|---|
| Nature | vector Lottie animation (gzip JSON) | VP9 pixel video |
| Cap | ≤ 64 KB | ≤ 256 KB, ≤ 3 s, 30 fps, no audio |
| `format` | `animated` | `video` |
| Build from | Lottie JSON/TGS only | any GIF/MP4/WEBM/image (ffmpeg) |

Detection is automatic: from the Bot API a sticker's `is_animated` / `is_video`
flags decide the format; downloaded files are also verified by magic bytes
(`1F 8B`→tgs, `1A 45 DF A3`→webm, PNG/RIFF-WEBP→static). A GIF or video **cannot**
become an *animated* emoji (those are vector-only) — it becomes a *video* emoji.

### Duplicate-proof by design

The old delete-and-rebuild churn is gone. The catalog (`collection/catalog.db`)
deduplicates at **ingest** time, not after publishing:

1. **`file_unique_id` pre-check** — a sticker already ingested is never
   downloaded again. Publishing also records the uploaded copies'
   `file_unique_id`s, so re-fetching your **own** published packs downloads
   nothing.
2. **Normalized content hash** — identical media from different packs collapse
   into one entry (their emoji/keywords/sources merge).
3. **Perceptual hash (dHash)** — near-identical logos merge within a threshold
   (opt-in via `--phash-threshold`; off by default so distinct look-alikes
   survive).
4. **Idempotent publish** — uploaded items are tracked per item; before
   uploading, the live set is reconciled and any applied-but-unrecorded sticker
   is attributed back to its catalog item, so interruptions can never create
   duplicates.
5. **Verified network retries** — a timeout after Telegram already processed an
   `addStickerToSet` is detected against the live set and never re-sent, so the
   same emoji can't land in a pack twice.

### Commands

```powershell
# 1. Download from one or more existing packs into the catalog
.venv\Scripts\python.exe fetch_pack.py https://t.me/addemoji/somepack_by_bot `
    --token-env GENERAL_BOT_TOKEN

# 2. (optional) Build extra emoji from your own files (auto static/video/animated)
.venv\Scripts\python.exe add_media.py --in input\myset --emoji 😀

# 3. Preview, then publish into new per-format packs (resumable)
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN --dry-run
.venv\Scripts\python.exe build_collection.py --base mypack --title "My Pack" `
    --token-env GENERAL_BOT_TOKEN
```

Sets are named `<base>s<n>_by_<bot>` (static), `<base>v<n>_by_<bot>` (video) and
`<base>a<n>_by_<bot>` (animated). All working data lives under `collection/`
(gitignored).

> Re-publishing other people's emoji under a new name may raise ownership /
> copyright concerns — only collect content you have the right to use.

## Emoji Mapper bot (premium-emoji ID extractor)

`emoji_bot.py` runs the general bot interactively (long-polling) and extracts
premium custom-emoji IDs with tap-to-copy buttons (Telegram `copy_text`):

- Send the bot a **premium emoji** → it replies with the ID on a copy button.
- Send/forward a **post mixing text + premium emoji** → it lists every ID;
  tapping a button copies them.
- **Add it to a channel/group** (as admin) → it DMs you the premium-emoji IDs
  from new posts. (Bots cannot read past channel history, only new posts.)

```powershell
.venv\Scripts\python.exe emoji_bot.py     # or run.ps1 -> C1
```

## Cloudflare Worker (both bots, hosted)

`worker/` runs the same bot behaviour on Cloudflare instead of your machine —
both bots in one Worker, each on its own path (`/tg/general`, `/tg/coin`) with
its own webhook secret, answering only the ids in `ADMIN_USER_IDS`. It also
exposes `POST /publish`, so a finished pack is announced **by the bot** in your
channel rather than by this machine: set `WORKER_PUBLISH_URL` and
`WORKER_PUBLISH_SECRET` in `.env` and `build_collection.py` routes its links
through it. Leave them unset and the existing direct path is used, unchanged.

> **A Telegram bot token can use `getUpdates` (polling) or a webhook — never
> both.** Registering a webhook for a token stops `emoji_bot.py` receiving
> anything on it; `deleteWebhook` hands it back. Run one or the other per token.

Setup, secrets and deployment: [`worker/README.md`](worker/README.md).

## Curate panel (pick which emoji go into the pack)

`panel.py` opens a local dark neon-blue web panel showing every emoji in the
catalog as a large labelled card. All cards are selected by default; click one
to toggle it (deselected = excluded from the next publish), Shift+click for a
range, **drag** to set the publish order. Visually similar emoji start out next
to each other so look-alikes are quick to deselect. Click **Save**, then
`build_collection.py` only publishes the included items.

Animated `.tgs` are **pre-rendered to animated WebP on the server** (rlottie)
and shown as plain `<img>`, so a grid of hundreds animates on the browser's
compositor with no animation library in the page. Only cards near the viewport
carry the animated frames; the rest hold a still. The header's **Animation:
On/Off** button stops that everywhere (remembered across reloads), and
`--preview-fps` sets the frame rate, which is the real lever on how heavy the
grid feels. Video cards still play on hover only.

```powershell
.venv\Scripts\python.exe panel.py        # or run.ps1 -> B4
.venv\Scripts\python.exe panel.py --preview-fps 12   # lighter still
```

## Crypto-coin workflow (one component: `coins/`)

The crypto-coin tool is now a self-contained component under `coins/`. It reuses
the shared engine at the project root (`build_pack.py`) and the coin bot
(`TELEGRAM_BOT_TOKEN`). Its data, scripts and images all live under `coins/`:

- `coins/fetch_logos.py` — download coin logos from CoinGecko + write `coins/keywords.csv`
- `coins/fetch_paprika.py` / `coins/fetch_cmc.py` — fill remaining coins from CoinPaprika / CoinMarketCap
- `coins/build_keywords.py` — (re)build `coins/keywords.csv` from logos on disk
- `coins/rebuild_dedup.py` — duplicate-proof full rebuild + inventory fill
- `coins/remap_ids.py` — rebuild `ticker_to_id.json` from image content, never positions
- `coins/enhance_map.py` / `coins/alias_map.py` — point chain-variant tickers at the base coin's id
- `coins/verify_logos.py` — review logos against official art; fix only the tickers you name
- `coins/write_manifests.py` — write a per-pack manifest (ticker + emoji id)
- `coins/check_all_packs.py` — audit every pack for blank/duplicate stickers
- `coins/run_convert.ps1` / `coins/run_rebuild_loop.ps1` — watchdog drivers for long runs

Extra dependency: `coins/remap_ids.py` imports numpy —
`pip install -r requirements-coins.txt`. Every other coin script runs on the core
manifest alone.

Convert coin logos to 100×100 PNGs (images live in `coins/logos/{svg,png}` →
`coins/logos/emoji`):

```powershell
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\svg --out coins\logos\emoji
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\png --out coins\logos\emoji
```

Then build/rebuild with the coin bot:

```powershell
# build + rebuild the id map + send the links (DESTRUCTIVE: deletes the old packs
# first). Subcommands: build = upload only, map = rebuild the id map, links = resend.
.venv\Scripts\python.exe coins\rebuild_dedup.py
```

## Project layout

```
Emoji Mapper/                  # the whole project
  build_pack.py                # core engine: upload any source dir with any bot
  make_emoji_pngs.py           # core engine: image -> 100x100 PNG (--in/--out)
  fetch_pack.py                # collector: download Telegram packs -> catalog
  fetch_emoji_ids.py           # collector: download specific emoji by id -> catalog
  add_media.py                 # collector: build emoji from scratch -> catalog
  build_collection.py          # collector: publish catalog -> new per-format packs
  panel.py                     # curate panel: pick & order what gets published
  emoji_bot.py                 # bot: extract premium-emoji ids (tap-to-copy)
  emojikit/                    # shared core toolkit
    logsetup.py                # UTC file logging
    media.py                   # format detect + hashing + static/video/tgs convert
    catalog.py                 # content-addressed SQLite catalog (dedup)
  worker/                      # Cloudflare Worker: both bots + /publish (TypeScript)
    src/                       # auth, telegram, emoji-id extraction, routing
    test/                      # vitest, fetch stubbed (never reaches Telegram)
  assets/                      # shipped images (incl. the brand logo)
  run.ps1                      # launcher (single-pack + collection workflows)
  scripts/check.ps1            # byte-compile + full unit suite (also used by CI)
  requirements.txt
  .env.example                 # configuration template
  README.md  SECURITY.md  LICENSE
  tests/                       # unit tests + fixtures (see tests/README.md)
  coins/                       # ONE component: the crypto-coin emoji tool
    fetch_*.py                 # coin logo fetchers (CoinGecko/Paprika/CMC)
    build_keywords.py
    rebuild_dedup.py           # duplicate-proof rebuild + inventory fill
    check_all_packs.py         # pack integrity audit
    run_convert.ps1 run_rebuild_loop.ps1
    keywords.csv               # coin ticker -> keywords (data)
    currency-emoji-inventory.md         # coin inventory (source)
    ticker_to_id.json shared_logo_groups.json
```

Generated/local-only (gitignored): `logos/` (and `coins/logos/`), `build/`,
`input/`, `collection/`, `*_state.json`, `*.filled.md`, `.env`, `secrets.md`.

## Checks

One command byte-compiles every source file, lints it, and runs the whole unit
suite — the same one CI runs, so local and CI results cannot drift:

```powershell
python -m pip install ruff   # once; ruff is not in the runtime manifests
.\scripts\check.ps1
.\run.ps1 -Check      # separate: environment doctor (venv/deps/ffmpeg/.env)
```

Lint is `ruff check .` with **no** arguments: `ruff.toml` at the repo root owns
the rule set, so nothing can diverge between CI and a local run. That set is
deliberately narrow — ruff's default rules plus `E402`, `BLE001`, `B` and
`RUF100` — because the source already carried ~120 `# noqa: E402` /
`# noqa: BLE001` comments written against a linter that was never configured.
Enabling exactly the codes those comments name is what makes them mean
something, and `RUF100` fails a `# noqa` that no longer suppresses anything, so
they cannot rot again.

Running the suite by hand? Use exactly this form:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"
```

`-t .` is not cosmetic. Without it the tests directory becomes the top level,
modules load as `test_x` instead of `tests.test_x`, and `tests/__init__.py` —
which scrubs credentials out of the environment and refuses non-loopback sockets
— never runs. See [`tests/README.md`](tests/README.md).

## Security

Tokens and the owner id live only in `.env` (never committed) and are read at
runtime. See [SECURITY.md](SECURITY.md). Revoke a leaked token via @BotFather
and update `.env`.

## License

Emoji Mapper is **proprietary** software — **All Rights Reserved**.
See [LICENSE](LICENSE). No use, copying, modification or distribution is
permitted without prior written permission from the copyright holder. Viewing
the source here does not grant any license. Third-party logos and data remain
subject to their own providers' licenses (see below).

## Sources & attribution

Coin vector logos come from open icon sets
([spothq/cryptocurrency-icons](https://github.com/spothq/cryptocurrency-icons),
[Cryptofonts/cryptoicons](https://github.com/Cryptofonts/cryptoicons)); raster
logos from [CoinGecko](https://www.coingecko.com/),
[CoinPaprika](https://coinpaprika.com/) and
[CoinMarketCap](https://coinmarketcap.com/). Respect each source's license and
terms when redistributing. For general packs, only use images you have the right
to use.
