/**
 * Pulling premium custom-emoji ids out of an update.
 *
 * A faithful port of `emoji_bot.extract_custom_emoji_ids`. The behaviour here
 * is not obvious and was learned the hard way, so it is restated rather than
 * left implicit:
 *
 *  - order is the order the emoji appear, across the whole message;
 *  - each real id is listed once, however many times it repeats;
 *  - quoted excerpts count. Per the Bot API, when a reply quotes PART of a
 *    message only a few entity types survive into `quote` -- custom_emoji is
 *    one of them -- so premium emoji inside the highlighted "> ..." block were
 *    silently dropped until `quote.entities` was scanned too. The same applies
 *    to `external_reply.quote` when the quoted message came from another chat.
 */

import type { TgMessage, TgMessageEntity } from "./types";

/**
 * A custom_emoji_id is a decimal id and nothing else.
 *
 * This is inbound data that gets interpolated into HTML Telegram parses (a
 * `<tg-emoji emoji-id=...>` attribute and a `<code>` block). Shape-checking at
 * the boundary is what keeps the sink safe; the length is not the point, the
 * "decimal only" is.
 */
const CUSTOM_EMOJI_ID = /^\d{1,25}$/;

/** Entity lists that can carry custom_emoji, including quoted excerpts. */
function entityLists(message: TgMessage): (TgMessageEntity[] | undefined)[] {
  // `quote` and `external_reply` are not in the typed surface this Worker
  // declares, because nothing else needs them -- read them structurally.
  const raw = message as unknown as {
    quote?: { entities?: TgMessageEntity[] };
    external_reply?: { quote?: { entities?: TgMessageEntity[] } };
  };
  return [
    message.entities,
    message.caption_entities,
    raw.quote?.entities,
    raw.external_reply?.quote?.entities,
  ];
}

export function extractCustomEmojiIds(message: TgMessage): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const list of entityLists(message)) {
    for (const ent of list ?? []) {
      if (ent.type !== "custom_emoji") continue;
      const cid = String(ent.custom_emoji_id ?? "");
      if (!CUSTOM_EMOJI_ID.test(cid)) continue;   // not an id; drop, do not render
      if (seen.has(cid)) continue;
      seen.add(cid);
      ids.push(cid);
    }
  }
  return ids;
}

/**
 * The reverse direction: ids typed as plain text rather than sent as emoji.
 *
 * The WHOLE message has to be ids and separators. A long number inside a
 * sentence is far more likely to be a chat id, a timestamp or a price than
 * something to look up, and answering prose with a wall of placeholder glyphs
 * is worse than ignoring it. Newline, comma, "comma space" and a bare single
 * id all parse; they are the shapes people actually paste.
 *
 * Kept in step with `emoji_bot.parse_id_list`.
 */
const ID_LIST = /^\d{15,25}(?:[\s,;]+\d{15,25})*$/;
const ID_SEP = /[\s,;]+/;

export function parseIdList(text: string): string[] {
  const body = (text ?? "").trim();
  if (!ID_LIST.test(body)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const part of body.split(ID_SEP)) {
    if (part && !seen.has(part)) {
      seen.add(part);
      out.push(part);
    }
  }
  return out;
}
