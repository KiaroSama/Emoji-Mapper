/**
 * The slice of the Bot API this Worker needs.
 *
 * Deliberately tiny. The Python engine keeps the hard part -- creating and
 * adding to sticker sets, with the verified-retry machinery that stops an
 * ambiguous upload becoming a duplicate. Nothing here mutates a pack; this
 * Worker only reads updates and sends messages, which are cheap to get right.
 */

const DEFAULT_API = "https://api.telegram.org";

export class BotApiError extends Error {
  constructor(readonly method: string, readonly description: string,
              readonly code?: number) {
    // The token is never part of the message: this string ends up in logs.
    super(`${method} failed (${code ?? "?"}): ${description}`);
    this.name = "BotApiError";
  }
}

export class Telegram {
  constructor(private readonly token: string,
              private readonly apiBase: string = DEFAULT_API) {}

  /**
   * POST one Bot API method.
   *
   * NOT retried. Every call this Worker makes is a sendMessage, which is not
   * idempotent and carries no dedup key, so a timeout after Telegram accepted
   * the message is indistinguishable from one before it -- retrying turns a
   * blip into two identical posts in the channel. The same reasoning is
   * written into the Python client, and this is the one place it could
   * silently diverge.
   */
  async call<T = unknown>(method: string, payload: Record<string, unknown>): Promise<T> {
    const res = await fetch(`${this.apiBase}/bot${this.token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let body: { ok?: boolean; result?: T; description?: string; error_code?: number };
    try {
      body = await res.json();
    } catch {
      throw new BotApiError(method, `non-JSON response (HTTP ${res.status})`, res.status);
    }
    if (!res.ok || body.ok !== true) {
      throw new BotApiError(method, body.description ?? `HTTP ${res.status}`,
                            body.error_code ?? res.status);
    }
    return body.result as T;
  }

  /**
   * Resolve custom-emoji ids to their stickers.
   *
   * The reverse lookup needs this rather than rendering an id straight into a
   * <tg-emoji> tag: Telegram falls back to the placeholder glyph for an id
   * that does not exist, so a typo would come back looking exactly like a
   * success. It also yields each sticker's own emoji, which is a far better
   * fallback than a fixed star wherever the tag cannot render -- notification
   * previews, copied-out text, older clients.
   *
   * Telegram caps one call at 200 ids; callers chunk.
   */
  getCustomEmojiStickers(ids: string[]): Promise<TgCustomEmojiSticker[]> {
    return this.call<TgCustomEmojiSticker[]>("getCustomEmojiStickers",
                                             { custom_emoji_ids: ids });
  }

  sendMessage(chatId: number | string, text: string,
              opts: Record<string, unknown> = {}): Promise<TgSentMessage> {
    return this.call<TgSentMessage>("sendMessage", {
      chat_id: chatId,
      text,
      // Long id lists are the point of this bot; a link preview per message
      // would bury them.
      disable_web_page_preview: true,
      ...opts,
    });
  }
}

export interface TgCustomEmojiSticker {
  custom_emoji_id: string;
  emoji?: string;
}

export interface TgSentMessage {
  message_id: number;
  chat: { id: number };
}

/** Escape text before it goes into a parse_mode=HTML message. */
export function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
