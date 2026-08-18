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
import { announce, handleUpdate } from "./handle";
import { Telegram } from "./telegram";
import type { BotName, Env, PublishRequest, TgUpdate } from "./types";

/** Telegram retries any non-2xx, so failures must be deliberate. */
const OK = () => new Response("ok");

function botConfig(env: Env, bot: BotName): { token: string; secret: string } {
  return bot === "general"
    ? { token: env.GENERAL_BOT_TOKEN, secret: env.GENERAL_WEBHOOK_SECRET }
    : { token: env.COIN_BOT_TOKEN, secret: env.COIN_WEBHOOK_SECRET };
}

async function onWebhook(request: Request, env: Env, bot: BotName): Promise<Response> {
  const { token, secret } = botConfig(env, bot);
  if (!verifyWebhook(request, secret)) {
    // 401, not 403: this is an authentication failure, and Telegram will not
    // retry a 4xx -- which is what we want for a request Telegram did not send.
    return new Response("unauthorized", { status: 401 });
  }
  if (!token) {
    console.error(`${bot}: no bot token configured`);
    return OK();          // 200: retrying will not conjure a token
  }

  let update: TgUpdate;
  try {
    update = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }

  const tg = new Telegram(token, env.TELEGRAM_API_BASE);
  const admins = parseAdmins(env.ADMIN_USER_IDS);
  try {
    const outcome = await handleUpdate(tg, update, admins);
    console.log(`${bot} ${update.update_id}: ${outcome}`);
  } catch (err) {
    // Swallow and return 200 on purpose. Telegram redelivers a failed update,
    // and every action this bot takes is a sendMessage -- a redelivery after a
    // partial success posts the same reply twice. The error is logged instead.
    console.error(`${bot} ${update.update_id} failed:`,
                  err instanceof Error ? err.message : String(err));
  }
  return OK();
}

async function onPublish(request: Request, env: Env): Promise<Response> {
  if (!verifyBearer(request, env.PUBLISH_SECRET)) {
    return new Response("unauthorized", { status: 401 });
  }
  let body: PublishRequest;
  try {
    body = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }
  if (!Array.isArray(body.packs) || body.packs.length === 0) {
    return Response.json({ ok: false, error: "packs must be a non-empty array" },
                         { status: 400 });
  }
  for (const p of body.packs) {
    // The set name goes into a public t.me link. Telegram set names are
    // [A-Za-z0-9_], so anything else is not one and must not be published.
    if (typeof p?.name !== "string" || !/^[A-Za-z0-9_]{1,64}$/.test(p.name)) {
      return Response.json({ ok: false, error: `bad pack name: ${String(p?.name)}` },
                           { status: 400 });
    }
  }

  const bot: BotName = body.bot === "general" ? "general" : "coin";
  const { token } = botConfig(env, bot);
  if (!token) return Response.json({ ok: false, error: `${bot} bot not configured` },
                                   { status: 500 });
  const chat = env.PACK_LINKS_CHAT_ID?.trim();
  if (!chat) return Response.json({ ok: false, error: "PACK_LINKS_CHAT_ID not set" },
                                  { status: 500 });

  const tg = new Telegram(token, env.TELEGRAM_API_BASE);
  try {
    const messageId = await announce(tg, chat, body);
    console.log(`announced ${body.packs.length} pack(s) as ${bot}`);
    return Response.json({ ok: true, message_id: messageId });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("announce failed:", msg);
    // Reported, never retried here: sendMessage is not idempotent, so the
    // caller decides -- and it can see from the channel whether it landed.
    return Response.json({ ok: false, error: msg }, { status: 502 });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return Response.json({ ok: true });
    }
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    switch (url.pathname) {
      case "/tg/general": return onWebhook(request, env, "general");
      case "/tg/coin":    return onWebhook(request, env, "coin");
      case "/publish":    return onPublish(request, env);
      default:            return new Response("not found", { status: 404 });
    }
  },
};
