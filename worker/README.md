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
both.** The moment you register a webhook for a token, `emojikit/emoji_bot.py` stops
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
| `GET /health` | none | Liveness + whether the log store is bound. No secrets. |

Each bot has its **own** path and its **own** webhook secret. One shared secret
would mean a leak from either bot could forge updates for the other.

## The two directions

Send the bot **premium emoji** and it replies with their ids. Send it **ids**
and it replies with the emoji — one per line, comma-separated, `, `-separated,
or a single id.

The whole message has to be ids and separators for the reverse direction to
trigger. A long number inside a sentence is far more likely to be a chat id or
a timestamp than something to look up.

Ids are resolved with `getCustomEmojiStickers` before anything is rendered.
A `<tg-emoji>` tag falls back to its placeholder glyph when the id does not
exist, so rendering an unchecked id makes a typo indistinguishable from a hit;
anything Telegram cannot resolve is named instead. Resolving also yields each
sticker's own emoji, which is the tag's fallback text: what shows up wherever
the custom emoji cannot be rendered - notification previews, copied-out text,
older clients - and better there than a column of identical stars. Viewing
custom emoji does not require Premium; only sending them does.

`emojikit/emoji_bot.py` carries the same behaviour (`parse_id_list`, `answer_typed_ids`)
so the poller and the Worker do not drift.

## Logs

The channel line follows the Ad Timer Bot's log format, with the bot tag on its
own first line:

```
[general]
❌ ERROR webhook
update 42: sendMessage failed (400): chat not found
2026-08-19 00:45:12 UTC
```

Every line starts with the bot that produced it — `[general]` or `[coin]` —
because both bots share this Worker, one log table and one channel, and a line
that does not say which one wrote it is not worth keeping.

The channel is capped at **12 messages a minute**. Over budget it drops and
*counts* rather than queueing — a queue inside a Worker isolate outlives the
request it belongs to and loses the messages anyway, just later and holding the
memory — and the dropped count rides on the next message that gets through, so
a burst is visible instead of silently swallowed. That budget is per isolate,
not global: it bounds one isolate handling a burst of redeliveries, not a fleet.

`LOG_CHAT_ID` accepts a channel id copied bare out of the Telegram UI
(`4211401345`) and adds the `-100` prefix the Bot API needs. Without that, the
failure is a bare "chat not found" far away from the setting that caused it.

**A channel post from the log channel is ignored.** Both bots administer it, so
every line posted there returns as a `channel_post` to both, and handling it
wrote another row about a message we had just written. Not a loop today — a
plain-text log line carries no custom emoji, so the handler returns before
sending — but it becomes one the moment channel handling grows a path that logs
an ERROR.

**D1**, capped at 10 MB, oldest evicted first. The eviction sums the whole
table, so it runs once every 250 rows rather than on every insert — keyed off
the `last_row_id` the insert just returned, not a counter in the isolate, which
would reset before it ever reached its threshold and let the table grow without
bound. Running it every time was affordable at 77 rows and ruinous at the cap:
10 MB holds roughly 163 000 rows, and D1's free tier allows 5 000 000 row reads
a day — about thirty log lines. The budget is exact when it runs; between runs
the table may sit up to 250 rows over it. The cap
counts the *text* stored, not the database file: D1 offers no cheap, reliable
file-size reading, and page overhead plus the index put the file somewhat above
it. One line's detail is capped at 2000 characters — a publish announcing 120
packs listed every name and cost ~6 KB by itself.

**Channel** (`LOG_CHAT_ID`), errors only. Level-based routing with WARNING
included was the obvious design and the wrong one: an unauthorised hit on a
public webhook URL is a WARNING, and a scanner walking the internet would turn
the channel into a firehose. Both bots must be administrators of it — each
posts its own lines, so the poster matches the tag.

Logging can never take the bots down: every sink failure is swallowed and
reported to `console`, which `wrangler tail` reads. `GET /health` reports
`log_db`, so a missing binding is visible without waiting for a line that will
never arrive.

```bash
npx wrangler d1 execute emoji-mapper-logs --remote \
  --command "SELECT ts, bot, level, event, detail FROM logs ORDER BY id DESC LIMIT 20"
```

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

Create the log database once, then deploy:

```powershell
npx wrangler d1 create emoji-mapper-logs      # put the id in wrangler.toml
npx wrangler d1 migrations apply emoji-mapper-logs --remote
npx wrangler deploy
```

Then point Telegram at it — one webhook per bot:

```powershell
.\scripts\set-webhooks.ps1 -BaseUrl https://<your-worker>.workers.dev
.\scripts\set-webhooks.ps1 -Status                    # confirm
.\scripts\set-webhooks.ps1 -Delete -Only general      # hand a token back
```

The script reads the same `.env` the secrets came from, so the registration and
the deployed secret cannot drift apart — a mismatch is silent: Telegram accepts
`setWebhook` happily and every delivery is then rejected 401, which looks
exactly like a dead bot. It refuses to replace a webhook that already points
somewhere else unless you pass `-Force`, and it says so when a token stops being
pollable.

The bots must be **administrators** of both the pack-links channel and the log
channel to post in them.

## Announcing from the local build

Set these two in `.env` and the publishers route announcements through the
Worker instead of talking to Telegram themselves:

```
WORKER_PUBLISH_URL=https://<your-worker>.workers.dev/publish
WORKER_PUBLISH_SECRET=<the PUBLISH_SECRET you set above>
```

Leave either unset and the old direct path is used, unchanged. Both or neither:
a URL without a secret is a half-finished setup that would 401 every
announcement, so it takes the direct path rather than pretending to work.

**All three publishers** go through one `emojikit.build_pack.announce_packs` —
`emojikit/build_pack.py` (single pack), `emojikit/build_collection.py` (collector) and
`coins/rebuild_dedup.py` (coin family). They used to carry three copies of
"format the link and sendMessage", and when this Worker arrived only the
collector learned about it, so a coin rebuild kept talking to Telegram from the
build machine while the owner believed the bot was posting. A test asserts all
three share the function.

The duplicate guard does not move: `state["sent"]` is what stops a re-run
announcing the same pack twice, and it holds whichever route sent it. A failed
announcement is **not** recorded as sent, so the next run retries it — and
neither route retries a `sendMessage` internally, because it is not idempotent
and has no dedup key, so a timeout after Telegram accepted the post cannot be
told from one before it.

A whole pack family goes in **one** `/publish` call, and the Worker splits it
across messages if it passes 4096 characters. A single message would be
rejected whole at that point, losing every link rather than just the overflow.

Body shape:

```json
{
  "bot": "coin",
  "note": "optional line above the list",
  "style": "cards",
  "packs": [{ "name": "gvcryptoemoji1_by_bot", "title": "Coins 1", "count": 200 }]
}
```

`style` picks the layout. **`cards`** (default) gives each pack a bold title
line with its link below — right for announcing one or two finished packs.
**`list`** is one line per pack, `title. url`, which is what a whole family of
29 reads as; as cards it is three screens of scrolling. It is explicit rather
than inferred from the pack count: the caller knows which it is announcing, and
a rule like "more than five means list" would silently change the look of a
real post. The direct (no-Worker) path renders the same two shapes, so turning
the Worker on cannot change how an announcement looks.

**Every field is checked before anything happens** (`src/validate.ts`). The
bearer proves *who* sent a body, never *what* is in it, and the TypeScript type
is only a compile-time claim about JSON nobody parsed. Three real defects went
through that gap: a `null` body threw `Cannot read properties of null` out of
the handler instead of answering 400; a misspelt `"genreal"` fell through the
`=== "general" ? … : "coin"` default and announced a general pack from the coin
bot's identity; and a string `count` reached the HTML Telegram parses.

Anything below is **400**, with a reason naming the field and the rule — and
nothing else happens: no token lookup, no message built, not even a log line
(which would itself be a `sendMessage` to the log channel).

| Field | Rule |
|---|---|
| the body | must be a JSON object — `null`, an array and a bare string are not |
| `packs` | a non-empty array, at most **500** entries |
| `packs[i]` | must be an object |
| `packs[i].name` | **required**, `[A-Za-z0-9_]{1,64}` — it becomes a public `t.me/addemoji/` link |
| `packs[i].title` / `.format` | absent, or a string of at most 4096 characters |
| `packs[i].count` | absent, or a non-negative safe integer — never a string |
| `bot` | absent (→ coin, the documented default), `"general"` or `"coin"` |
| `style` | absent (→ cards), `"cards"` or `"list"` |
| `note` | absent, or a string of at most 4096 characters |

`null` is refused rather than read as "unset": every caller sends real values, so
a `null` is a bug upstream and a loud 400 finds it. The `note` cap matters
because the note is one block and the renderer only splits *between* blocks — an
over-long one used to leave as a single message Telegram rejects, so half the
announcement landed and the rest was dropped mid-send. `packs` is bounded
because it is caller-shaped input, and unbounded work inside a Worker is a
request that never finishes. `test/publish-input.test.ts` covers each rule.

## Development

```bash
npm run typecheck     # tsc --noEmit
npm test              # vitest, no network
npm run dev           # wrangler dev
```

The tests stub `fetch`, so nothing in them can reach Telegram — the same rule
the Python suite enforces, for the same reason: a test once reached live
Telegram and replaced a sticker in a published pack.

## Errors never carry a credential

Every Bot API request URL embeds the bot token (`/bot<token>/sendMessage`), and
a transport failure names the URL it was attempting. That message travelled out
of `Telegram.call`, through the publish handler's catch, into the HTTP 502 body
**and** into every log sink — so a caller holding only the publish bearer could
be handed the bot token instead.

`src/redact.ts` is the one redactor, and everything on its way out goes through
it. Three independent rules, because each covers a hole the others do not:

| rule | catches |
|---|---|
| `/bot…/` URL shape | a token this Worker never had in `env` — another bot, a stale deployment, a token quoted inside an upstream description |
| bare `digits:secret` shape | a token outside a URL |
| the configured `env` literals | a secret with no recognisable shape, such as a webhook secret echoed back |

The credential env values are listed explicitly rather than derived by scanning
`env`: a binding added later must be considered rather than assumed harmless,
and a chat id or api base must stay readable in the messages that need it.

`errText` also flattens an error's `cause` chain before redacting — `String(err)`
hides a cause and `JSON.stringify` of an Error yields `{}`, so an unsanitised
cause is both invisible and liable to surface wherever something serialises it
more thoroughly. `logging.ts` keeps its own length cap, which is a storage-budget
concern and nothing to do with credentials.

`test/redaction.test.ts` asserts that a synthetic, correctly-shaped token appears
in no response body, no log line and no nested cause — while a successful
publish and an ordinary refusal still read normally.

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
