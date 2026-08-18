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
