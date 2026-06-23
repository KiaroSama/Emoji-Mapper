# Emoji Mapper

Build Telegram **premium custom-emoji packs** from any collection of images.

Emoji Mapper started as a crypto-coin logo packer and is now a general tool: it
takes a folder of images, normalizes each one to the exact 100×100 PNG that
Telegram custom emoji require, and uploads them through a bot into one or more
custom-emoji sets owned by your account.

It ships with two independent workflows that share the same engine:

| Workflow | Bot | Source | Purpose |
|----------|-----|--------|---------|
| **Crypto coins** | `TELEGRAM_BOT_TOKEN` | CoinGecko / CoinPaprika / CoinMarketCap logos | the original coin-logo packs |
| **General** | `GENERAL_BOT_TOKEN` (`@GodVerifyEmojiMapperbot`) | any folder of images you provide | any non-coin emoji pack |

The same scripts (`make_emoji_pngs.py` + `build_pack.py`) power both; only the
source folder and the selected bot token differ.

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
- Dependencies in `requirements.txt` (`pip install -r requirements.txt`)

## Setup

```powershell
# 1. Create a virtual environment and install deps
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

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
| `--per-set` | 400 | emojis per set (Telegram hard cap is 200) |
| `--limit` / `--start` | 0 / 0 | process a slice of the source |
| `--state` | `state_<base>.json` | resume file (per pack, never clobbered) |
| `--dry-run` | off | validate inputs without calling Telegram |

Runs are **resumable**: progress is saved to `state_<base>.json`, so an
interrupted or flood-limited run continues without recreating existing sets.

## Crypto-coin workflow (one component: `coins/`)

The crypto-coin tool is now a self-contained component under `coins/`. It reuses
the shared engine at the project root (`build_pack.py`) and the coin bot
(`TELEGRAM_BOT_TOKEN`). Its data, scripts and images all live under `coins/`:

- `coins/fetch_logos.py` — download coin logos from CoinGecko + write `coins/keywords.csv`
- `coins/fetch_paprika.py` / `coins/fetch_cmc.py` — fill remaining coins from CoinPaprika / CoinMarketCap
- `coins/build_keywords.py` — (re)build `coins/keywords.csv` from logos on disk
- `coins/rebuild_packs.py` / `coins/rebuild_dedup.py` — duplicate-proof full rebuild + inventory fill
- `coins/check_all_packs.py` — audit every pack for blank/duplicate stickers
- `coins/run_convert.ps1` / `coins/run_rebuild_loop.ps1` — watchdog drivers for long runs

Convert coin logos to 100×100 PNGs (images live in `coins/logos/{svg,png}` →
`coins/logos/emoji`):

```powershell
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\svg --out coins\logos\emoji
.venv\Scripts\python.exe make_emoji_pngs.py --in coins\logos\png --out coins\logos\emoji
```

Then build/rebuild with the coin bot:

```powershell
.venv\Scripts\python.exe coins\rebuild_packs.py
```

## Project layout

```
Emoji Mapper/                  # the whole project
  build_pack.py                # core engine: upload any source dir with any bot
  make_emoji_pngs.py           # core engine: image -> 100x100 PNG (--in/--out)
  run.ps1                      # launcher (general + coin workflows)
  requirements.txt
  .env.example                 # configuration template
  README.md  SECURITY.md
  coins/                       # ONE component: the crypto-coin emoji tool
    fetch_*.py                 # coin logo fetchers (CoinGecko/Paprika/CMC)
    build_keywords.py
    rebuild_packs.py rebuild_dedup.py   # duplicate-proof rebuild + inventory fill
    check_all_packs.py         # pack integrity audit
    run_convert.ps1 run_rebuild_loop.ps1
    keywords.csv               # coin ticker -> keywords (data)
    currency-emoji-inventory.md         # coin inventory (source)
    ticker_to_id.json shared_logo_groups.json
```

Generated/local-only (gitignored): `logos/` (and `coins/logos/`), `build/`,
`input/`, `*_state.json`, `*.filled.md`, `.env`, `secrets.md`.

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
