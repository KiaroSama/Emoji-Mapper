/**
 * Who may talk to this Worker, and who the bots answer.
 *
 * Two separate gates, and they are not interchangeable:
 *   - `verifyWebhook` proves a request really came from Telegram.
 *   - `isAdmin` decides whether the human behind an update gets a reply.
 *
 * Both fail CLOSED. An unset or malformed allowlist means "nobody", never
 * "everybody" -- the same rule the Python bot's `allowed_user_ids()` follows,
 * because a misconfiguration must not silently open the bot to the world.
 */

/** Constant-time string compare, so a wrong secret cannot be found byte by byte. */
function timingSafeEqual(a: string, b: string): boolean {
  const ab = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  // Length is not secret (and differing lengths cannot match), but the compare
  // below must still run over a fixed number of bytes.
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

/**
 * True when the request carries the secret Telegram was told to send.
 *
 * Telegram echoes `secret_token` from setWebhook in this header on every
 * delivery. Without it the webhook URL alone is the only thing standing between
 * a stranger and a forged update, and URLs leak (logs, proxies, screenshots).
 */
export function verifyWebhook(request: Request, expected: string | undefined): boolean {
  if (!expected) return false;   // not configured -> refuse, do not wave through
  const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
  return got !== null && timingSafeEqual(got, expected);
}

/** Bearer check for the local publisher endpoint. */
export function verifyBearer(request: Request, expected: string | undefined): boolean {
  if (!expected) return false;
  const header = request.headers.get("Authorization") ?? "";
  const prefix = "Bearer ";
  if (!header.startsWith(prefix)) return false;
  return timingSafeEqual(header.slice(prefix.length), expected);
}

/**
 * Parse the admin allowlist.
 *
 * Accepts commas or semicolons, ignores blanks, and drops anything that is not
 * a positive integer id. Returns an empty set when unset -- callers must treat
 * that as "answer nobody".
 */
export function parseAdmins(raw: string | undefined): Set<number> {
  const out = new Set<number>();
  for (const part of (raw ?? "").replace(/;/g, ",").split(",")) {
    const t = part.trim();
    if (!t) continue;
    // Number() would accept "12.5", "0x10", " 12 " and "1e3"; a bot allowlist
    // must only ever hold plain positive integers.
    if (!/^\d+$/.test(t)) continue;
    const n = Number(t);
    if (Number.isSafeInteger(n) && n > 0) out.add(n);
  }
  return out;
}

export function isAdmin(userId: number | undefined, admins: Set<number>): boolean {
  return userId !== undefined && admins.has(userId);
}
