# Emoji Mapper bots — Cloudflare Worker

Both bots in one Worker, answering only the admins you name, plus the endpoint
the local builder calls so a finished pack is announced **by the bot** in your
channel.

```
local build (Python)  ──POST /publish (bearer)──►  Worker  ──Bot API──►  channel
                                                     ▲
                                    Telegram ────────┘  POST /tg/general
                                                        POST /tg/coin
```

## Read this before deploying

**A Telegram bot token can use `getUpdates` (polling) or a webhook — never
both.** The moment you register a webhook for a token, `emoji_bot.py` stops
receiving anything on it. That is not a bug and it is not this Worker's choice;
it is how the Bot API works. Run one or the other per token.

To hand a token back to the local poller:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

Nothing else in the project changes: the Python engine still owns creating packs
and adding stickers, with the verified-retry machinery that keeps an ambiguous
upload from becoming a duplicate. This Worker only reads updates and sends
messages.

## Routes

| Route | Auth | What |
|---|---|---|
| `POST /tg/general` | `X-Telegram-Bot-Api-Secret-Token` | Webhook for the general bot |
| `POST /tg/coin` | `X-Telegram-Bot-Api-Secret-Token` | Webhook for the coin bot |
| `POST /publish` | `Authorization: Bearer …` | Announce finished packs in the channel |
| `GET /health` | none | Liveness. Returns no secrets. |

Each bot has its **own** path and its **own** webhook secret. One shared secret
would mean a leak from either bot could forge updates for the other.

## Setup

```powershell
cd worker
npm install
.\scripts\put-secrets.ps1 -DryRun    # shows which key comes from where
.\scripts\put-secrets.ps1            # pushes them all from ..\.env
```

That script takes every value the Worker needs straight out of `.env` and pipes
it to `wrangler secret put` **through stdin** — no value is printed, stored in
shell history, or passed as an argument (arguments are visible in the process
list). It reports key names and character counts only.

It exists because two of these are easy to get wrong by hand:

- `ADMIN_USER_IDS` is **composed** from `PACK_OWNER_USER_ID` +
  `BOT_ALLOWED_USER_IDS`. The list fails closed, so a typo means the bots answer
  nobody and nothing tells you why.
- `GENERAL_WEBHOOK_SECRET`, `COIN_WEBHOOK_SECRET` and `PUBLISH_SECRET` are
  generated (32 random bytes) if `.env` has none — and written **back** to
  `.env`, because a second run that minted different ones would break every
  webhook delivery's secret check.

Doing it by hand instead:

```bash
wrangler secret put GENERAL_BOT_TOKEN
wrangler secret put COIN_BOT_TOKEN
wrangler secret put GENERAL_WEBHOOK_SECRET   # a long random string
wrangler secret put COIN_WEBHOOK_SECRET      # a DIFFERENT long random string
wrangler secret put PUBLISH_SECRET           # bearer for /publish
wrangler secret put ADMIN_USER_IDS           # e.g. 123456789,987654321
wrangler secret put PACK_LINKS_CHAT_ID       # "@yourchannel" or "-100…"
```

The channel is a secret rather than a `[vars]` entry — not because it is a
credential, but because `wrangler.toml` is committed and that would publish the
channel name. Both arrive as `env.PACK_LINKS_CHAT_ID` at runtime, so it costs
nothing.

`ADMIN_USER_IDS` **fails closed**: unset, empty, or all-invalid means the bots
answer nobody. That is deliberate — a misconfiguration must not open the bots to
everyone. Only plain positive integers are accepted, so `0x10`, `12.5` and `1e3`
are ignored rather than silently coerced.

Deploy, then point Telegram at it:

```bash
wrangler deploy

curl -X POST "https://api.telegram.org/bot<GENERAL_TOKEN>/setWebhook" \
  -d "url=https://<your-worker>.workers.dev/tg/general" \
  -d "secret_token=<GENERAL_WEBHOOK_SECRET>"

curl -X POST "https://api.telegram.org/bot<COIN_TOKEN>/setWebhook" \
  -d "url=https://<your-worker>.workers.dev/tg/coin" \
  -d "secret_token=<COIN_WEBHOOK_SECRET>"
```

The bot must be an **administrator** of the channel to post in it.

## Announcing from the local build

Set these two in `.env` and the publishers route announcements through the
Worker instead of talking to Telegram themselves:

```
WORKER_PUBLISH_URL=https://<your-worker>.workers.dev/publish
WORKER_PUBLISH_SECRET=<the PUBLISH_SECRET you set above>
```

Leave them unset and the old direct path is used, unchanged.

The duplicate guard does not move: `state["sent"]` is what stops a re-run
announcing the same pack twice, and it holds whichever route sent it. A failed
announcement is **not** recorded as sent, so the next run retries it — and
neither route retries a `sendMessage` internally, because it is not idempotent
and has no dedup key, so a timeout after Telegram accepted the post cannot be
told from one before it.

Body shape:

```json
{
  "bot": "coin",
  "note": "optional line above the list",
  "packs": [{ "name": "cryptoemoji1_by_bot", "title": "Coins 1", "count": 200 }]
}
```

`name` must match `[A-Za-z0-9_]{1,64}` — it goes into a public `t.me/addemoji/`
link, so anything else is refused rather than published.

## Development

```bash
npm run typecheck     # tsc --noEmit
npm test              # vitest, no network
npm run dev           # wrangler dev
```

The tests stub `fetch`, so nothing in them can reach Telegram — the same rule
the Python suite enforces, for the same reason: a test once reached live
Telegram and replaced a sticker in a published pack.

## Behaviour worth knowing

- **Webhook handlers return 200 even when handling fails.** Telegram redelivers
  any non-2xx, and every action here is a `sendMessage`, so a redelivery after a
  partial success posts the same reply twice. Failures are logged instead.
- **A stranger gets one reply in private and silence in a group** — answering in
  groups would make the bot a spam vector.
- **`custom_emoji_id` is shape-checked** before it reaches the HTML that
  Telegram parses. It is inbound data interpolated into a `tg-emoji` attribute;
  only decimal ids are rendered.
- **Long id lists are split** across messages under Telegram's 4096-character
  limit rather than being rejected.
