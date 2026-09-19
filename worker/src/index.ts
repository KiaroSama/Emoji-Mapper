/**
 * One Worker, both bots.
 *
 * Routes:
 *   POST /tg/general   Telegram webhook for @YourEmojiBot
 *   POST /tg/coin      Telegram webhook for @YourCoinEmojiBot
 *   POST /publish      the local builder announcing a finished pack
 *   GET  /health       liveness, no secrets
 *
 * Each bot gets its own path and its own webhook secret rather than one
 * endpoint sniffing which token sent it: the token never appears in the
 * request, so sniffing would mean guessing, and a shared secret would let a
 * leak from one bot forge updates for the other.
 *
 * IMPORTANT deployment note, because it is not reversible by accident: a
 * Telegram token can use getUpdates OR a webhook, never both. Registering a
 * webhook here stops `emoji_bot.py` receiving anything on that token. Run one
 * or the other, and see worker/README.md for how to hand a token back.
 */

import { parseAdmins, verifyBearer, verifyWebhook } from "./auth";
import { announce, handleUpdate, LOG_ECHO } from "./handle";
import { log } from "./logging";
import { Telegram } from "./telegram";
import { errText } from "./redact";
import { validatePublishRequest } from "./validate";
import type { BotName, Env, TgUpdate } from "./types";

/** Telegram retries any non-2xx, so failures must be deliberate. */
const OK = () => new Response("ok");

function botConfig(env: Env, bot: BotName): { token: string; secret: string } {
  return bot === "general"
    ? { token: env.GENERAL_BOT_TOKEN, secret: env.GENERAL_WEBHOOK_SECRET }
    : { token: env.COIN_BOT_TOKEN, secret: env.COIN_WEBHOOK_SECRET };
}

/** JSON parsing does not prove the required envelope, even after authentication.
 * Only validate fields this dispatcher requires; future update types stay valid.
 */
function isUpdateEnvelope(value: unknown): value is TgUpdate {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const id = (value as Record<string, unknown>).update_id;
  return typeof id === "number" && Number.isSafeInteger(id) && id >= 0;
}

async function onWebhook(request: Request, env: Env, ctx: ExecutionContext,
                         bot: BotName): Promise<Response> {
  const { token, secret } = botConfig(env, bot);
  if (!verifyWebhook(request, secret)) {
    // 401 identifies an authentication failure. Do not assume that Telegram
    // suppresses retries on 4xx; no authenticated action is allowed here.
    // Recorded, but never forwarded to the channel: this URL is public, and a
    // scanner walking the internet would otherwise flood it.
    ctx.waitUntil(log(env, { bot, level: "WARNING", event: "unauthorized",
                             detail: "webhook secret did not match" }));
    return new Response("unauthorized", { status: 401 });
  }
  if (!token) {
    ctx.waitUntil(log(env, { bot, level: "ERROR", event: "misconfigured",
                             detail: "no bot token bound" }));
    return OK();          // 200: retrying will not conjure a token
  }

  let update: unknown;
  try {
    update = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }

  if (!isUpdateEnvelope(update)) {
    return new Response("bad request", { status: 400 });
  }

  const tg = new Telegram(token, env.TELEGRAM_API_BASE);
  const admins = parseAdmins(env.ADMIN_USER_IDS);
  try {
    const outcome = await handleUpdate(tg, update, admins, env.LOG_CHAT_ID);
    // Our own log line coming back from the channel. Recording it would write a
    // row about a row -- two of them, since both bots administer that channel.
    if (outcome !== LOG_ECHO) {
      ctx.waitUntil(log(env, { bot, level: "INFO", event: "webhook",
                               detail: `update ${update.update_id}: ${outcome}` }));
    }
  } catch (err) {
    // Swallow and return 200 on purpose. Telegram redelivers a failed update,
    // and every action this bot takes is a sendMessage -- a redelivery after a
    // partial success posts the same reply twice. The error is logged instead,
    // and this one IS worth a channel message: nothing else will report it.
    ctx.waitUntil(log(env, { bot, level: "ERROR", event: "webhook",
                             detail: `update ${update.update_id}: ${errText(err, env)}` }));
  }
  return OK();
}

async function onPublish(request: Request, env: Env,
                         ctx: ExecutionContext): Promise<Response> {
  if (!verifyBearer(request, env.PUBLISH_SECRET)) {
    return new Response("unauthorized", { status: 401 });
  }
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }
  // Checked before a single property is READ: `null.packs` used to throw a
  // TypeError straight out of this handler, and the bearer only ever proved
  // who the caller was, never that the body was one.
  const checked = validatePublishRequest(raw);
  if (!checked.ok) {
    return Response.json({ ok: false, error: checked.error }, { status: 400 });
  }
  const body = checked.value;

  // The documented default. An invalid value can no longer reach it.
  const bot: BotName = body.bot ?? "coin";
  const { token } = botConfig(env, bot);
  if (!token) return Response.json({ ok: false, error: `${bot} bot not configured` },
                                   { status: 500 });
  const chat = env.PACK_LINKS_CHAT_ID?.trim();
  if (!chat) return Response.json({ ok: false, error: "PACK_LINKS_CHAT_ID not set" },
                                  { status: 500 });

  const tg = new Telegram(token, env.TELEGRAM_API_BASE);
  try {
    const messageIds = await announce(tg, chat, body);
    ctx.waitUntil(log(env, {
      bot, level: "INFO", event: "publish",
      detail: `${body.packs.length} pack(s) in ${messageIds.length} message(s): ` +
              body.packs.map((p) => p.name).join(", "),
    }));
    return Response.json({ ok: true, message_ids: messageIds });
  } catch (err) {
    const msg = errText(err, env);
    ctx.waitUntil(log(env, { bot, level: "ERROR", event: "publish", detail: msg }));
    // Reported, never retried here: sendMessage is not idempotent, so the
    // caller decides -- and it can see from the channel whether it landed.
    return Response.json({ ok: false, error: msg }, { status: 502 });
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      // Reports whether the log store is bound, so a missing binding is
      // visible without waiting for a log line that will never arrive.
      return Response.json({ ok: true, log_db: Boolean(env.DB) });
    }
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    switch (url.pathname) {
      case "/tg/general": return onWebhook(request, env, ctx, "general");
      case "/tg/coin":    return onWebhook(request, env, ctx, "coin");
      case "/publish":    return onPublish(request, env, ctx);
      default:            return new Response("not found", { status: 404 });
    }
  },
};
