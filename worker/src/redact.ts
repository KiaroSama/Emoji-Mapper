/**
 * Strip credentials out of anything on its way to a caller or a log.
 *
 * Every Bot API request URL embeds the token (`/bot<token>/sendMessage`), and a
 * transport failure puts the URL it was attempting into its message. That
 * message travelled out of `Telegram.call`, through the publish handler's
 * catch, into the HTTP 502 body AND into every log sink -- so a caller holding
 * only the publish bearer could be handed the bot token instead.
 *
 * Two independent passes, because either alone leaves a hole:
 *
 * - the URL shape catches a token this Worker never had in `env` (a different
 *   bot, a stale deployment, a token embedded in an upstream description);
 * - the configured literals catch a secret that appears without the `/bot…/`
 *   shape around it, such as a webhook secret quoted back in an error.
 *
 * Ordering matters: the shape pass runs first so a configured token inside a
 * URL is replaced once, as part of the URL, rather than leaving `/bot[REDACTED]/`
 * assembled from two rules.
 */

import type { Env } from "./types";

export const MASK = "[REDACTED]";

/** `…/bot<token>/method` -> `…/bot[REDACTED]/method`, for any token shape. */
const BOT_URL = /\/bot[A-Za-z0-9_:.\-]+/g;

/**
 * A bare Telegram bot token: digits, a colon, then the secret.
 *
 * Moved here from `logging.ts` rather than copied, so there is one redactor
 * and not two that drift. Its comment there is worth keeping: an earlier
 * version matched a looser shape and passed every test until one used a real
 * URL.
 */
const BARE_TOKEN = /\d{6,12}:[A-Za-z0-9_-]{30,}/g;

/**
 * The env values that are credentials. Listed explicitly rather than derived
 * by scanning `env`: a binding added later must be considered, not silently
 * assumed harmless, and non-secret values (a chat id, an api base) must not be
 * masked out of messages that need them to be readable.
 */
export function secretsOf(env: Partial<Env> | undefined): string[] {
  if (!env) return [];
  const keys: (keyof Env)[] = [
    "GENERAL_BOT_TOKEN", "COIN_BOT_TOKEN",
    "GENERAL_WEBHOOK_SECRET", "COIN_WEBHOOK_SECRET",
    "PUBLISH_SECRET",
  ];
  return keys
    .map((k) => env[k])
    .filter((v): v is string => typeof v === "string" && v.length >= 8);
}

/** Redact a string. Safe on anything; never throws. */
export function redact(text: string, env?: Partial<Env>): string {
  let out = String(text ?? "")
    .replace(BOT_URL, `/bot${MASK}`)
    .replace(BARE_TOKEN, MASK);
  for (const secret of secretsOf(env)) {
    out = out.split(secret).join(MASK);
  }
  return out;
}

/**
 * A message from an unknown throwable, redacted, with its cause chain flattened.
 *
 * `String(err)` on an Error hides a `cause`, and `JSON.stringify` of an Error
 * yields `{}` -- so an unsanitised cause is both invisible here and liable to
 * surface wherever something serialises it more thoroughly. Walking the chain
 * makes what we are redacting explicit, and the depth bound keeps a cyclic
 * cause from spinning.
 */
export function errText(err: unknown, env?: Partial<Env>): string {
  const parts: string[] = [];
  let cur: unknown = err;
  for (let depth = 0; cur !== undefined && cur !== null && depth < 5; depth++) {
    parts.push(cur instanceof Error ? cur.message : String(cur));
    cur = cur instanceof Error ? (cur as Error & { cause?: unknown }).cause : undefined;
  }
  return redact(parts.join(": "), env);
}
