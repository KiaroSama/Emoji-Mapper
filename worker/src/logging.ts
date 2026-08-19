/**
 * Where this Worker's log lines go: a D1 table held under a byte budget, and --
 * for the ones worth interrupting someone for -- a Telegram channel.
 *
 * Two rules shape everything here:
 *
 * 1. LOGGING MUST NEVER BREAK THE BOT. Every failure in this file is swallowed.
 *    A D1 outage or a misconfigured log channel must not turn a working reply
 *    into a dropped update, and it must not make the webhook return non-2xx --
 *    Telegram would redeliver, and the reply would be posted twice.
 * 2. EVERY LINE NAMES ITS BOT FIRST. Both bots share this Worker, this table
 *    and this channel; a line that does not say which one produced it is not
 *    worth storing.
 */

import { Telegram } from "./telegram";
import type { BotName, Env } from "./types";

/**
 * Byte budget for the log table, oldest evicted first.
 *
 * This counts the TEXT actually stored (bot + level + event + detail), not the
 * database file: D1 gives no cheap, reliable file-size reading, and page
 * overhead plus the primary-key index mean the file will sit somewhat above
 * this. It is an honest bound on what we put in, not a promise about what
 * SQLite writes out.
 */
export const LOG_BYTES_CAP = 10 * 1024 * 1024;

export type LogLevel = "INFO" | "WARNING" | "ERROR";

export interface LogEntry {
  bot: BotName;
  level: LogLevel;
  /** Short, stable, greppable. e.g. "webhook", "publish", "unauthorized". */
  event: string;
  /** Free text. Never a token -- see redact(). */
  detail?: string;
  /**
   * Force channel delivery on or off. Default: ERROR only.
   *
   * Level-based routing with WARNING included was the obvious design and the
   * wrong one: an unauthorised hit on a public webhook URL is a WARNING, and a
   * scanner walking the internet would have turned the log channel into a
   * firehose. The channel is for things a person must see.
   */
  toChannel?: boolean;
}

/**
 * Delete the oldest rows until the stored text fits the budget.
 *
 * Rows are summed newest-first; every row whose running total has already
 * passed the cap is older than the budget allows, and `id` is monotonic, so one
 * `id <= MAX(...)` covers all of them. Recomputed from the table each time
 * rather than tracked in a counter, so it cannot drift out of step with reality
 * after a failed write. COALESCE keeps the delete a no-op while under budget:
 * ids start at 1, so nothing is `<= -1`.
 */
const EVICT_SQL = `
DELETE FROM logs WHERE id <= COALESCE((
  SELECT MAX(id) FROM (
    SELECT id, SUM(bytes) OVER (ORDER BY id DESC) AS running FROM logs
  ) WHERE running > ?1
), -1)`;

const INSERT_SQL =
  "INSERT INTO logs (ts, bot, level, event, detail, bytes) VALUES (?1,?2,?3,?4,?5,?6)";

/**
 * Anything token-shaped, in case a Bot API error echoes a URL back at us.
 *
 * NO `\b` before the digits. A token reaches us as `.../bot123456789:AAH...`,
 * and `t` and `1` are both word characters -- so a word boundary is exactly
 * what is NOT there in the one position that matters. The first version had it
 * and passed every test until one used a real URL shape.
 */
const TOKEN_RE = /\d{6,12}:[A-Za-z0-9_-]{30,}/g;

/**
 * Cap on one line's detail.
 *
 * A publish announcing 120 packs listed every name: ~6 KB for a single INFO
 * row, which spends the whole 10 MB budget in under two thousand lines. The
 * cap belongs here rather than at each call site, so the next caller that
 * builds a long string cannot reintroduce it.
 */
const DETAIL_LIMIT = 2000;

export function redact(s: string): string {
  const out = s.replace(TOKEN_RE, "[REDACTED]");
  return out.length > DETAIL_LIMIT
    ? `${out.slice(0, DETAIL_LIMIT)}… (+${out.length - DETAIL_LIMIT} chars)`
    : out;
}

const LEVEL_EMOJI: Record<LogLevel, string> = { INFO: "ℹ️", WARNING: "⚠️", ERROR: "❌" };

/** Telegram's limit is 4096; a log line longer than this is not readable on a phone. */
const MAX_CHANNEL_CHARS = 700;

/**
 * Telegram shows a channel's *internal* id (4211401345) in several places, but
 * the Bot API only accepts the -100-prefixed form. Accepting the bare number
 * means a copy-paste out of the Telegram UI works instead of failing later with
 * a bare "chat not found". Borrowed from the Ad Timer Bot, which hit exactly
 * that. Returns null when the value is unusable or logging is off.
 */
export function normalizeChatId(value: string | undefined | null): string | null {
  const text = String(value ?? "").trim();
  if (!text || text === "0") return null;
  if (text.startsWith("@")) return text.length > 1 ? text : null;
  if (!/^-?\d+$/.test(text)) return null;
  if (text.startsWith("-")) return text;          // already a channel/group id
  return `-100${text}`;
}

function utcStamp(atMs: number): string {
  return new Date(atMs).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}

/**
 * One rendered entry, in the Ad Timer Bot's log-channel shape.
 *
 *   [general]
 *   ❌ ERROR webhook
 *   update 42: sendMessage failed (400): chat not found
 *   2026-08-19 00:45:12 UTC
 *
 * The bot tag is its own line by owner request: two bots share this channel and
 * the tag is the first thing you look for when scanning it on a phone.
 */
export function formatLine(e: LogEntry, atMs: number = Date.now(),
                           suppressed = 0): string {
  const lines = [`[${e.bot}]`, `${LEVEL_EMOJI[e.level]} ${e.level} ${e.event}`];
  if (e.detail) lines.push(redact(e.detail));
  lines.push(utcStamp(atMs));
  if (suppressed > 0) lines.push(`(+${suppressed} suppressed by rate limit)`);
  const text = lines.join("\n");
  return text.length > MAX_CHANNEL_CHARS
    ? `${text.slice(0, MAX_CHANNEL_CHARS - 1)}…` : text;
}

/**
 * Telegram allows roughly 20 messages a minute to one chat; stay well under it.
 *
 * Over budget the sink DROPS and counts rather than queueing -- an unbounded
 * queue inside a Worker isolate outlives the request it belongs to and still
 * loses the messages, just later and with the memory held. The dropped count
 * rides along on the next message that gets through, so a burst is visible
 * rather than silently swallowed. Also borrowed from the Ad Timer Bot.
 *
 * Honest limit: this state is per ISOLATE, not global. It bounds the realistic
 * flood -- one isolate handling a burst of redeliveries -- not a fleet of them.
 */
const CHANNEL_MAX_PER_WINDOW = 12;
const CHANNEL_WINDOW_MS = 60_000;
const rate = { windowStartMs: 0, sentInWindow: 0, suppressed: 0 };

async function writeToD1(env: Env, e: LogEntry): Promise<void> {
  if (!env.DB) return;
  const detail = e.detail ? redact(e.detail) : null;
  // Byte length, not character count: a Persian or emoji-bearing detail costs
  // more than its .length suggests, and undercounting would push the table
  // past the budget it is supposed to hold.
  const bytes = new TextEncoder().encode(
    `${e.bot}${e.level}${e.event}${detail ?? ""}`).length;
  await env.DB.batch([
    env.DB.prepare(INSERT_SQL).bind(Date.now(), e.bot, e.level, e.event, detail, bytes),
    env.DB.prepare(EVICT_SQL).bind(LOG_BYTES_CAP),
  ]);
}

async function writeToChannel(env: Env, e: LogEntry, atMs: number): Promise<void> {
  const chat = normalizeChatId(env.LOG_CHAT_ID);
  if (!chat) return;
  // The bot that produced the line posts it, so the channel shows the same
  // origin the line claims. Both bots are administrators of it.
  const token = e.bot === "general" ? env.GENERAL_BOT_TOKEN : env.COIN_BOT_TOKEN;
  if (!token) return;

  if (atMs - rate.windowStartMs >= CHANNEL_WINDOW_MS) {
    rate.windowStartMs = atMs;
    rate.sentInWindow = 0;
  }
  if (rate.sentInWindow >= CHANNEL_MAX_PER_WINDOW) {
    rate.suppressed += 1;
    return;
  }
  rate.sentInWindow += 1;
  // Claim the carried count BEFORE the send: if the send fails the count is
  // lost, which is the right trade -- carrying it forever would make the next
  // successful message claim suppressions that never happened.
  const carried = rate.suppressed;
  rate.suppressed = 0;
  const tg = new Telegram(token, env.TELEGRAM_API_BASE);
  await tg.sendMessage(chat, formatLine(e, atMs, carried));
}

/**
 * Record one line. Returns a promise the caller should hand to
 * `ctx.waitUntil()` so the response is not delayed by a database round trip.
 *
 * Never rejects.
 */
export function log(env: Env, e: LogEntry): Promise<void> {
  const toChannel = e.toChannel ?? e.level === "ERROR";
  const atMs = Date.now();
  // console.log stays as well: it is the only sink that survives a D1 outage,
  // and `wrangler tail` reads it.
  console.log(formatLine(e, atMs));
  const jobs = [writeToD1(env, e)];
  if (toChannel) jobs.push(writeToChannel(env, e, atMs));
  return Promise.allSettled(jobs).then((results) => {
    for (const r of results) {
      if (r.status === "rejected") {
        console.error(`[${e.bot}] ERROR log-sink`,
                      r.reason instanceof Error ? r.reason.message : String(r.reason));
      }
    }
  });
}
