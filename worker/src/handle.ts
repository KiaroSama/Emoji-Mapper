/**
 * What each bot does with an update, and how a finished pack reaches the
 * channel.
 *
 * Both bots run the same handler. The coin bot has never had an interactive
 * side -- it exists as the identity that owns the gvcryptoemoji* packs -- but
 * giving it the same behaviour costs nothing and means a message sent to the
 * wrong bot still gets an answer instead of silence.
 */

import { escapeHtml, Telegram } from "./telegram";
import { extractCustomEmojiIds, parseIdList } from "./emoji";
import { isAdmin } from "./auth";
import type { PublishRequest, TgMessage, TgUpdate } from "./types";

const DENIED_TEXT =
  "This is a private bot and you are not on its access list.\n" +
  "If you should have access, ask the owner to add your numeric Telegram user id.";

const START_TEXT =
  "Send me any message containing <b>premium (custom) emoji</b> and I will " +
  "reply with their ids.\n\nWorks with emoji in the text, in a caption, and " +
  "inside a quoted reply.\n\nOr the other way round: <b>send me ids</b> and I " +
  "show you the emoji. One per line, comma-separated, or a single id.";

/** Telegram rejects a sendMessage over 4096 characters. */
const TEXT_LIMIT = 4096;

/**
 * Outcome meaning "this was our own log line coming back; record nothing".
 *
 * Exported so the router matches a constant instead of a repeated string
 * literal -- a typo there would silently reinstate the noise.
 */
export const LOG_ECHO = "log-echo";

/** Split ids into messages that stay under the limit. */
export function renderIdMessages(ids: string[], header?: string,
                                 glyphs?: Map<string, string>): string[] {
  if (ids.length === 0) {
    return [header ? `${header}\nNo premium emoji in that message.`
                   : "No premium emoji in that message."];
  }
  const out: string[] = [];
  let buf = header ? `${header}\n` : "";
  for (const id of ids) {
    // <tg-emoji> renders the emoji itself; the <code> block is what you copy.
    // The sticker's OWN emoji when we know it. This text is the FALLBACK the
    // tag carries: it is what shows up wherever the custom emoji cannot be
    // rendered -- notification previews, copied-out text, older clients, and
    // the pack being deleted later. A column of identical stars says nothing
    // in any of those places. (Viewing custom emoji does NOT need Premium;
    // only sending them does.)
    const glyph = escapeHtml(glyphs?.get(id) ?? "⭐");
    const line = `<tg-emoji emoji-id="${id}">${glyph}</tg-emoji> <code>${id}</code>\n`;
    if (buf.length + line.length > TEXT_LIMIT) {
      out.push(buf.trimEnd());
      buf = "";
    }
    buf += line;
  }
  if (buf.trim()) out.push(buf.trimEnd());
  return out;
}

async function reply(tg: Telegram, chatId: number | string, ids: string[],
                     opts: { replyTo?: number; header?: string } = {}): Promise<void> {
  const parts = renderIdMessages(ids, opts.header);
  for (const [i, text] of parts.entries()) {
    await tg.sendMessage(chatId, text, {
      parse_mode: "HTML",
      // Only the first part answers the original message; the rest follow it.
      ...(i === 0 && opts.replyTo ? { reply_to_message_id: opts.replyTo } : {}),
    });
  }
}

/** Telegram resolves at most 200 ids per call. */
const RESOLVE_CHUNK = 200;

/**
 * Reverse lookup: the user typed ids, so show them the emoji.
 *
 * Resolved through getCustomEmojiStickers rather than rendered straight into a
 * <tg-emoji> tag, because Telegram silently falls back to the placeholder glyph
 * for an id that does not exist -- so a typo would come back looking exactly
 * like a success. An id it cannot resolve is named instead.
 */
async function replyToTypedIds(tg: Telegram, chatId: number | string,
                               ids: string[], replyTo?: number): Promise<string> {
  const glyphs = new Map<string, string>();
  for (let i = 0; i < ids.length; i += RESOLVE_CHUNK) {
    const found = await tg.getCustomEmojiStickers(ids.slice(i, i + RESOLVE_CHUNK));
    for (const st of found ?? []) {
      if (st?.custom_emoji_id) glyphs.set(st.custom_emoji_id, st.emoji ?? "⭐");
    }
  }
  const known = ids.filter((id) => glyphs.has(id));
  const unknown = ids.filter((id) => !glyphs.has(id));
  let header: string | undefined;
  if (unknown.length) {
    const shown = unknown.slice(0, 10).map((id) => `<code>${id}</code>`).join(", ");
    const more = unknown.length > 10 ? ` (+${unknown.length - 10} more)` : "";
    header = `⚠️ Telegram does not know ${unknown.length} of these: ${shown}${more}`;
  }
  if (known.length === 0) {
    await tg.sendMessage(chatId, header ?? "No usable ids in that message.", {
      parse_mode: "HTML",
      ...(replyTo ? { reply_to_message_id: replyTo } : {}),
    });
    return `typed:0/${ids.length}`;
  }
  for (const [n, text] of renderIdMessages(known, header, glyphs).entries()) {
    await tg.sendMessage(chatId, text, {
      parse_mode: "HTML",
      ...(n === 0 && replyTo ? { reply_to_message_id: replyTo } : {}),
    });
  }
  return `typed:${known.length}/${ids.length}`;
}

/**
 * Handle one update.
 *
 * Returns a short string describing what happened, for logging. Nothing here
 * ever includes a token or the full message text.
 */
export async function handleUpdate(tg: Telegram, update: TgUpdate,
                                   admins: Set<number>,
                                   logChatId?: string): Promise<string> {
  const msg: TgMessage | undefined = update.message ?? update.edited_message;
  if (msg) {
    const sender = msg.from?.id;
    if (!isAdmin(sender, admins)) {
      // Answer once in private so a real person is not left guessing; stay
      // silent in groups, so the bot cannot be used to spam them.
      if (msg.chat.type === "private") {
        try {
          await tg.sendMessage(msg.chat.id, DENIED_TEXT);
        } catch {
          // A denial is best-effort: failing to send one must not retry the
          // update, which would just try to deny the same person again.
        }
      }
      return `denied:${msg.chat.type}`;
    }
    const text = msg.text ?? "";
    if (/^\/(start|help|menu)\b/.test(text)) {
      await tg.sendMessage(msg.chat.id, START_TEXT, { parse_mode: "HTML" });
      return "greeted";
    }
    // Ids typed as text and premium emoji cannot both be the subject of one
    // message: a message that is nothing but digits and separators carries no
    // custom_emoji entity to extract.
    const typed = parseIdList(text);
    if (typed.length) {
      return await replyToTypedIds(tg, msg.chat.id, typed, msg.message_id);
    }
    const ids = extractCustomEmojiIds(msg);
    await reply(tg, msg.chat.id, ids, { replyTo: msg.message_id });
    return `ids:${ids.length}`;
  }

  const post = update.channel_post;
  if (post) {
    // Both bots administer the log channel, so every line this Worker posts
    // there comes straight back as a channel_post -- to BOTH of them. Handling
    // it writes two more rows about a message we just wrote, and if channel
    // handling ever grows a path that logs an ERROR, that is a feedback loop
    // fed by its own output. Found by watching real traffic, not by reasoning.
    if (logChatId && String(post.chat.id) === logChatId.trim()) return LOG_ECHO;
    const ids = extractCustomEmojiIds(post);
    if (ids.length === 0) return "channel:none";
    // Channel posts have no sender to authorise, so the answer goes to the
    // admins rather than back into the channel.
    const title = escapeHtml(post.chat.title ?? "");
    for (const admin of admins) {
      await reply(tg, admin, ids, { header: `From channel <b>${title}</b>:` });
    }
    return `channel:${ids.length}`;
  }
  return "ignored";
}

/**
 * A title long enough to matter is a bug upstream, not something to render.
 * Cut it rather than let one pack push a whole announcement over the limit.
 */
const TITLE_LIMIT = 200;

/**
 * The announcement(s) the local builder asks this Worker to post.
 *
 * Returns a LIST for the same reason renderIdMessages does: the coin rebuild
 * announces its whole family in one call (30+ packs today) and a collector run
 * can publish more, so a single message would eventually hit the 4096-character
 * limit and Telegram would reject the entire announcement -- losing every link,
 * not just the overflow. Packs are never split mid-entry: a half-written
 * t.me/addemoji link is worse than a second message.
 */
export function renderAnnouncement(req: PublishRequest): string[] {
  const blocks: string[] = [];
  const list = req.style === "list";
  if (req.note) blocks.push(escapeHtml(req.note));
  for (const p of req.packs) {
    const title = escapeHtml((p.title ?? p.name).slice(0, TITLE_LIMIT));
    const count = p.count !== undefined ? ` — ${p.count}` : "";
    // addemoji is the install link for a custom-emoji set.
    const url = `https://t.me/addemoji/${encodeURIComponent(p.name)}`;
    blocks.push(list ? `${title}. ${url}`
                     : `✅ <b>${title}</b>${count}\n${url}`);
  }

  const out: string[] = [];
  let buf = "";
  for (const block of blocks) {
    const candidate = buf ? `${buf}\n${block}` : block;
    if (buf && candidate.length > TEXT_LIMIT) {
      out.push(buf);
      buf = block;
    } else {
      buf = candidate;
    }
  }
  if (buf) out.push(buf);
  return out;
}

/**
 * Post the announcement and return every message id.
 *
 * Not transactional, and cannot be: if part 2 fails after part 1 landed, the
 * caller's `state["sent"]` has not recorded the pack, so a re-run announces it
 * again. That is the deliberate trade -- a duplicate link is visible and
 * harmless, a silently missing one is not.
 */
export async function announce(tg: Telegram, chatId: number | string,
                               req: PublishRequest): Promise<number[]> {
  const ids: number[] = [];
  for (const text of renderAnnouncement(req)) {
    const sent = await tg.sendMessage(chatId, text, { parse_mode: "HTML" });
    ids.push(sent.message_id);
  }
  return ids;
}
