/**
 * Runtime validation for POST /publish.
 *
 * The bearer proves WHO sent a body, never WHAT is in it, and
 * `PublishRequest` is only a compile-time claim about JSON nobody checked.
 * Three things went through that gap: a `null` body threw
 * `Cannot read properties of null (reading 'packs')` out of the handler
 * instead of answering 400; a misspelt `"genreal"` fell through the
 * `=== "general" ? ... : "coin"` default and announced a general pack from
 * the coin bot; and a string `count` reached the HTML Telegram parses.
 *
 * Everything is refused here, before a token is looked up or a message built,
 * so an invalid payload has no side effects at all -- not even a log line,
 * which would itself be a sendMessage to the log channel.
 */

import { TEXT_LIMIT } from "./telegram";
import type { BotName, PublishedPack, PublishRequest } from "./types";

/**
 * Most packs one call may announce.
 *
 * The coin family is ~30 today and a collector run can publish more, so the
 * bound is generous. It exists because `packs` is caller-shaped input and
 * unbounded work inside a Worker is a request that never finishes.
 */
const MAX_PACKS = 500;

/** A Telegram sticker-set name and nothing else: it goes into a public link. */
const SET_NAME = /^[A-Za-z0-9_]{1,64}$/;

export type Checked =
  | { ok: true; value: PublishRequest }
  | { ok: false; error: string };

const bad = (error: string): Checked => ({ ok: false, error });

/** `typeof null` and `typeof []` are both "object"; neither is a body. */
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * Absent, or free text no longer than one whole message.
 *
 * Absent means the key is missing. `null` is refused rather than read as
 * "unset": the callers all send real strings, so a null is a bug upstream and
 * a loud 400 finds it, where a silent default would not.
 */
function okText(v: unknown): v is string | undefined {
  return v === undefined || (typeof v === "string" && v.length <= TEXT_LIMIT);
}

/** Absent, or a whole non-negative number. */
function okCount(v: unknown): v is number | undefined {
  return v === undefined
    || (typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
}

function okBot(v: unknown): v is BotName | undefined {
  return v === undefined || v === "general" || v === "coin";
}

function okStyle(v: unknown): v is "cards" | "list" | undefined {
  return v === undefined || v === "cards" || v === "list";
}

/**
 * Check an unknown body and hand back the normalized request, or the reason.
 *
 * The reason names the field and the rule. It never echoes the value back at
 * the caller and never carries an internal error string, because it is a
 * response body.
 */
export function validatePublishRequest(raw: unknown): Checked {
  if (!isRecord(raw)) return bad("body must be a JSON object");

  const packs = raw.packs;
  if (!Array.isArray(packs) || packs.length === 0) {
    return bad("packs must be a non-empty array");
  }
  if (packs.length > MAX_PACKS) {
    return bad(`packs must hold at most ${MAX_PACKS} entries`);
  }

  const checked: PublishedPack[] = [];
  for (let i = 0; i < packs.length; i++) {
    const at = `packs[${i}]`;
    const p: unknown = packs[i];
    if (!isRecord(p)) return bad(`${at} must be an object`);
    const name = p.name;
    // This is the one field that becomes a public t.me/addemoji link, so
    // anything that is not a Telegram set name must not be published.
    if (typeof name !== "string" || !SET_NAME.test(name)) {
      return bad(`${at}.name must match [A-Za-z0-9_]{1,64}`);
    }
    if (!okText(p.title)) {
      return bad(`${at}.title must be a string of at most ${TEXT_LIMIT} characters`);
    }
    if (!okText(p.format)) {
      return bad(`${at}.format must be a string of at most ${TEXT_LIMIT} characters`);
    }
    if (!okCount(p.count)) return bad(`${at}.count must be a non-negative integer`);
    checked.push({ name, title: p.title, format: p.format, count: p.count });
  }

  const bot = raw.bot;
  // The documented default stays: an OMITTED bot announces as the coin bot.
  // A value that was actually provided is checked, because "genreal" used to
  // reach that same default and post from the wrong identity.
  if (!okBot(bot)) return bad('bot must be "general" or "coin"');
  const style = raw.style;
  if (!okStyle(style)) return bad('style must be "cards" or "list"');
  const note = raw.note;
  // The note is one block and the renderer only ever splits BETWEEN blocks,
  // so an over-long one used to leave as a single message Telegram rejects.
  // Refusing it here means the caller learns instead of half the announcement
  // landing and the rest being dropped mid-send.
  if (!okText(note)) {
    return bad(`note must be a string of at most ${TEXT_LIMIT} characters`);
  }

  return { ok: true, value: { bot, packs: checked, note, style } };
}
