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
import { extractCustomEmojiIds } from "./emoji";
import { isAdmin } from "./auth";
import type { PublishRequest, TgMessage, TgUpdate } from "./types";

const DENIED_TEXT =
  "This is a private bot and you are not on its access list.\n" +
  "If you should have access, ask the owner to add your numeric Telegram user id.";

const START_TEXT =
  "Send me any message containing <b>premium (custom) emoji</b> and I will " +
  "reply with their ids.\n\nWorks with emoji in the text, in a caption, and " +
  "inside a quoted reply.";

/** Telegram rejects a sendMessage over 4096 characters. */
const TEXT_LIMIT = 4096;

/** Split ids into messages that stay under the limit. */
export function renderIdMessages(ids: string[], header?: string): string[] {
  if (ids.length === 0) {
    return [header ? `${header}\nNo premium emoji in that message.`
                   : "No premium emoji in that message."];
  }
  const out: string[] = [];
  let buf = header ? `${header}\n` : "";
  for (const id of ids) {
    // <tg-emoji> renders the emoji itself; the <code> block is what you copy.
    const line = `<tg-emoji emoji-id="${id}">⭐</tg-emoji> <code>${id}</code>\n`;
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

/**
 * Handle one update.
 *
 * Returns a short string describing what happened, for logging. Nothing here
 * ever includes a token or the full message text.
 */
export async function handleUpdate(tg: Telegram, update: TgUpdate,
                                   admins: Set<number>): Promise<string> {
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
    const ids = extractCustomEmojiIds(msg);
    await reply(tg, msg.chat.id, ids, { replyTo: msg.message_id });
    return `ids:${ids.length}`;
  }

  const post = update.channel_post;
  if (post) {
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

/** The announcement the local builder asks this Worker to post. */
export function renderAnnouncement(req: PublishRequest): string {
  const lines: string[] = [];
  if (req.note) lines.push(escapeHtml(req.note), "");
  for (const p of req.packs) {
    const title = escapeHtml(p.title ?? p.name);
    const count = p.count !== undefined ? ` — ${p.count}` : "";
    // addemoji is the install link for a custom-emoji set.
    lines.push(`✅ <b>${title}</b>${count}\nhttps://t.me/addemoji/${encodeURIComponent(p.name)}`);
  }
  return lines.join("\n");
}

export async function announce(tg: Telegram, chatId: number | string,
                               req: PublishRequest): Promise<number> {
  const text = renderAnnouncement(req);
  const sent = await tg.sendMessage(chatId, text, { parse_mode: "HTML" });
  return sent.message_id;
}
