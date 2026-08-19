/** Only the parts of the Bot API this Worker actually reads. */

export interface Env {
  /** Bot tokens. Secrets -- never logged, never echoed in a response. */
  GENERAL_BOT_TOKEN: string;
  COIN_BOT_TOKEN: string;
  /** Per-bot webhook secrets, echoed by Telegram on every delivery. */
  GENERAL_WEBHOOK_SECRET: string;
  COIN_WEBHOOK_SECRET: string;
  /** Bearer the local publisher must present to announce a pack. */
  PUBLISH_SECRET: string;
  /** Comma-separated numeric Telegram user ids allowed to use the bots. */
  ADMIN_USER_IDS: string;
  /** Channel the finished-pack announcements go to: "-100..." or "@name". */
  PACK_LINKS_CHAT_ID: string;
  /**
   * Channel that receives important log lines (errors). Optional: unset means
   * D1 and `wrangler tail` only, which is a quieter setup, not a broken one.
   * Both bots must be administrators of it -- each posts its own lines.
   */
  LOG_CHAT_ID?: string;
  /**
   * Log table. Optional so the Worker still serves if the binding is missing;
   * logging degrades to console, it does not take the bots down.
   */
  DB?: D1Database;
  /** Overridable for tests; defaults to the real Bot API. */
  TELEGRAM_API_BASE?: string;
}

export interface TgUser {
  id: number;
  is_bot?: boolean;
  first_name?: string;
  username?: string;
}

export interface TgChat {
  id: number;
  type: "private" | "group" | "supergroup" | "channel";
  title?: string;
}

export interface TgMessageEntity {
  type: string;
  offset: number;
  length: number;
  custom_emoji_id?: string;
}

export interface TgMessage {
  message_id: number;
  from?: TgUser;
  chat: TgChat;
  text?: string;
  caption?: string;
  entities?: TgMessageEntity[];
  caption_entities?: TgMessageEntity[];
  sticker?: { custom_emoji_id?: string; emoji?: string; set_name?: string };
  reply_to_message?: TgMessage;
}

export interface TgUpdate {
  update_id: number;
  message?: TgMessage;
  edited_message?: TgMessage;
  channel_post?: TgMessage;
}

/** Which bot an update arrived for. Kept explicit so replies never cross bots. */
export type BotName = "general" | "coin";

/** One published pack, as the local builder reports it. */
export interface PublishedPack {
  /** Telegram set name, e.g. "cryptoemoji1_by_YourCoinEmojiBot". */
  name: string;
  /** Human title shown in the announcement. */
  title?: string;
  /** static | video | animated -- optional, only used for grouping the text. */
  format?: string;
  /** How many emoji the set holds, when the builder knows it. */
  count?: number;
}

export interface PublishRequest {
  /** Which bot posts the announcement. Defaults to "coin". */
  bot?: BotName;
  packs: PublishedPack[];
  /** Optional free-text line placed above the list. */
  note?: string;
}
