/**
 * The parts that must not be wrong: who gets in, and what the bot says back.
 *
 * No network. The Bot API is a stub, so nothing here can reach Telegram --
 * the same rule the Python suite enforces with its own hermetic guard, and for
 * the same reason: a test once reached live Telegram and replaced a sticker in
 * a published pack.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import worker from "../src/index";
import { parseAdmins, verifyBearer, verifyWebhook } from "../src/auth";
import { extractCustomEmojiIds } from "../src/emoji";
import { renderAnnouncement, renderIdMessages } from "../src/handle";
import { formatLine, log, redact } from "../src/logging";
import type { Env, TgMessage } from "../src/types";

const ENV: Env = {
  GENERAL_BOT_TOKEN: "111:general",
  COIN_BOT_TOKEN: "222:coin",
  GENERAL_WEBHOOK_SECRET: "general-hook-secret",
  COIN_WEBHOOK_SECRET: "coin-hook-secret",
  PUBLISH_SECRET: "publish-bearer",
  ADMIN_USER_IDS: "42, 77",
  PACK_LINKS_CHAT_ID: "@testchannel",
  TELEGRAM_API_BASE: "https://api.telegram.invalid",
};

/**
 * `waitUntil` runs the job instead of deferring it, so a test can await the
 * logging a route fired and assert on what it wrote. Deferring it would make
 * every log assertion a race.
 */
const pending: Promise<unknown>[] = [];
const CTX = {
  waitUntil: (p: Promise<unknown>) => { pending.push(p); },
  passThroughOnException: () => {},
} as unknown as ExecutionContext;
const settle = () => Promise.allSettled(pending.splice(0));

/** A D1 stand-in that records the SQL and bindings it was handed. */
function stubDb() {
  const runs: { sql: string; args: unknown[] }[] = [];
  const prepare = (sql: string) => ({
    bind: (...args: unknown[]) => ({ sql, args, run: async () => ({}) }),
  });
  return {
    db: {
      prepare,
      batch: async (stmts: { sql: string; args: unknown[] }[]) => {
        runs.push(...stmts);
        return [];
      },
    } as unknown as D1Database,
    runs,
  };
}

/** Records every Bot API call instead of making one. */
function stubApi() {
  const calls: { method: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    const method = String(url).split("/").pop() ?? "";
    calls.push({ method, body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1, chat: { id: 1 } } }),
                        { headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

function webhookReq(path: string, secret: string, update: unknown): Request {
  return new Request(`https://w.dev${path}`, {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret,
               "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
}

const msgFrom = (id: number, extra: Partial<TgMessage> = {}): TgMessage => ({
  message_id: 5,
  from: { id },
  chat: { id: 900, type: "private" },
  ...extra,
});

beforeEach(() => vi.unstubAllGlobals());

describe("the allowlist fails closed", () => {
  it("treats unset and empty as nobody, never everybody", () => {
    expect(parseAdmins(undefined).size).toBe(0);
    expect(parseAdmins("").size).toBe(0);
    expect(parseAdmins("  , ; ").size).toBe(0);
  });

  it("accepts only plain positive integers", () => {
    // Number() would take every one of these; an allowlist must not.
    const admins = parseAdmins("42, 0x10, 12.5, 1e3, -7, 0, abc; 77");
    expect([...admins].sort((a, b) => a - b)).toEqual([42, 77]);
  });
});

describe("webhook authentication", () => {
  it("refuses a request with no secret configured", () => {
    const r = webhookReq("/tg/general", "anything", {});
    expect(verifyWebhook(r, undefined)).toBe(false);
    expect(verifyWebhook(r, "")).toBe(false);
  });

  it("refuses a wrong or missing header", () => {
    expect(verifyWebhook(webhookReq("/tg/general", "wrong", {}), "right")).toBe(false);
    const bare = new Request("https://w.dev/tg/general", { method: "POST" });
    expect(verifyWebhook(bare, "right")).toBe(false);
  });

  it("accepts the exact secret", () => {
    expect(verifyWebhook(webhookReq("/tg/general", "right", {}), "right")).toBe(true);
  });

  it("does not let one bot's secret authenticate the other", async () => {
    const calls = stubApi();
    const res = await worker.fetch(
      webhookReq("/tg/coin", ENV.GENERAL_WEBHOOK_SECRET,
                 { update_id: 1, message: msgFrom(42) }), ENV, CTX);
    expect(res.status).toBe(401);
    expect(calls).toHaveLength(0);
  });
});

describe("only admins get answered", () => {
  it("denies a stranger in private, and sends nothing else", async () => {
    const calls = stubApi();
    const res = await worker.fetch(
      webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET,
                 { update_id: 2, message: msgFrom(999) }), ENV, CTX);
    expect(res.status).toBe(200);          // 200, or Telegram redelivers forever
    expect(calls).toHaveLength(1);
    expect(String(calls[0].body.text)).toContain("not on its access list");
  });

  it("stays completely silent to a stranger in a group", async () => {
    const calls = stubApi();
    await worker.fetch(webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET, {
      update_id: 3,
      message: msgFrom(999, { chat: { id: -100, type: "supergroup", title: "G" } }),
    }), ENV, CTX);
    // Answering here would make the bot a spam vector in any group it is in.
    expect(calls).toHaveLength(0);
  });

  it("answers an admin", async () => {
    const calls = stubApi();
    await worker.fetch(webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET, {
      update_id: 4,
      message: msgFrom(42, {
        text: "hi",
        entities: [{ type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "5899781975" }],
      }),
    }), ENV, CTX);
    expect(calls).toHaveLength(1);
    expect(String(calls[0].body.text)).toContain("5899781975");
  });
});

describe("custom emoji extraction", () => {
  it("keeps order, de-duplicates, and reads quoted excerpts", () => {
    const msg = {
      message_id: 1, chat: { id: 1, type: "private" },
      entities: [
        { type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "111" },
        { type: "bold", offset: 2, length: 1 },
        { type: "custom_emoji", offset: 3, length: 2, custom_emoji_id: "111" },
        { type: "custom_emoji", offset: 5, length: 2, custom_emoji_id: "222" },
      ],
      caption_entities: [
        { type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "333" },
      ],
      quote: { entities: [
        { type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "444" },
      ] },
      external_reply: { quote: { entities: [
        { type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "555" },
      ] } },
    } as unknown as TgMessage;
    expect(extractCustomEmojiIds(msg)).toEqual(["111", "222", "333", "444", "555"]);
  });

  it("drops anything that is not a decimal id", () => {
    // This value is interpolated into HTML Telegram parses, so the shape check
    // at this boundary is what keeps the sink safe.
    const msg = {
      message_id: 1, chat: { id: 1, type: "private" },
      entities: [
        { type: "custom_emoji", offset: 0, length: 1, custom_emoji_id: '"><b>x' },
        { type: "custom_emoji", offset: 1, length: 1, custom_emoji_id: "12a34" },
        { type: "custom_emoji", offset: 2, length: 1, custom_emoji_id: "" },
        { type: "custom_emoji", offset: 3, length: 1, custom_emoji_id: "777" },
      ],
    } as unknown as TgMessage;
    expect(extractCustomEmojiIds(msg)).toEqual(["777"]);
  });
});

describe("replies stay under Telegram's message limit", () => {
  it("splits a long id list instead of being rejected", () => {
    const ids = Array.from({ length: 500 }, (_, i) => String(1000000000 + i));
    const parts = renderIdMessages(ids);
    expect(parts.length).toBeGreaterThan(1);
    for (const p of parts) expect(p.length).toBeLessThanOrEqual(4096);
    // Nothing may be lost in the split.
    const joined = parts.join("\n");
    for (const id of ids) expect(joined).toContain(id);
  });
});

describe("publishing a finished pack", () => {
  const body = {
    packs: [{ name: "gvcryptoemoji1_by_bot", title: "Coins 1", count: 200 }],
  };

  it("refuses without the bearer", async () => {
    const calls = stubApi();
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST", body: JSON.stringify(body),
    }), ENV, CTX);
    expect(res.status).toBe(401);
    expect(calls).toHaveLength(0);
  });

  it("refuses a pack name that is not a Telegram set name", async () => {
    const calls = stubApi();
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST",
      headers: { Authorization: `Bearer ${ENV.PUBLISH_SECRET}` },
      // This ends up in a public t.me link; a path escape must not reach it.
      body: JSON.stringify({ packs: [{ name: "../../evil" }] }),
    }), ENV, CTX);
    expect(res.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("posts the pack link to the configured channel", async () => {
    const calls = stubApi();
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST",
      headers: { Authorization: `Bearer ${ENV.PUBLISH_SECRET}` },
      body: JSON.stringify(body),
    }), ENV, CTX);
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].body.chat_id).toBe("@testchannel");
    expect(String(calls[0].body.text)).toContain("t.me/addemoji/gvcryptoemoji1_by_bot");
  });

  it("escapes a title so it cannot inject markup", () => {
    const parts = renderAnnouncement({ packs: [{ name: "ok_set", title: "<b>x</b>&" }] });
    expect(parts.join("\n")).toContain("&lt;b&gt;x&lt;/b&gt;&amp;");
  });

  it("splits a whole pack family instead of losing every link to one rejection", () => {
    // The coin rebuild announces its entire family in one call. A single
    // message would eventually pass 4096 characters and Telegram rejects the
    // WHOLE thing -- so the overflow costs every link, not just the last one.
    const packs = Array.from({ length: 120 }, (_, i) => ({
      name: `gvcryptoemoji${i + 1}_by_GodVerifyCoinEmojiMapperbot`,
      title: `Crypto pack number ${i + 1}`,
    }));
    const parts = renderAnnouncement({ packs, note: "all packs:" });
    expect(parts.length).toBeGreaterThan(1);
    for (const p of parts) expect(p.length).toBeLessThanOrEqual(4096);
    const joined = parts.join("\n");
    for (const p of packs) expect(joined).toContain(p.name);
  });

  it("never splits a pack entry away from its link", () => {
    const packs = Array.from({ length: 200 }, (_, i) => ({ name: `set_${i}`, title: `T${i}` }));
    for (const part of renderAnnouncement({ packs })) {
      // Every ✅ line in a part must be followed by its own URL in that part.
      const ticks = (part.match(/✅/g) ?? []).length;
      const links = (part.match(/t\.me\/addemoji\//g) ?? []).length;
      expect(links).toBe(ticks);
    }
  });

  it("returns one message id per part", async () => {
    const calls = stubApi();
    const packs = Array.from({ length: 120 }, (_, i) => ({
      name: `gvcryptoemoji${i + 1}_by_GodVerifyCoinEmojiMapperbot`,
      title: `Crypto pack number ${i + 1}`,
    }));
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST",
      headers: { Authorization: `Bearer ${ENV.PUBLISH_SECRET}` },
      body: JSON.stringify({ packs }),
    }), ENV, CTX);
    const out = await res.json() as { ok: boolean; message_ids: number[] };
    expect(out.ok).toBe(true);
    expect(out.message_ids).toHaveLength(calls.length);
    expect(calls.length).toBeGreaterThan(1);
  });
});

describe("logging", () => {
  it("names the bot first, in D1 and in the channel alike", async () => {
    expect(formatLine({ bot: "coin", level: "ERROR", event: "publish" }))
      .toBe("[coin] ERROR publish");
    expect(formatLine({ bot: "general", level: "INFO", event: "webhook", detail: "x" }))
      .toBe("[general] INFO webhook\nx");
  });

  it("keeps a bot token out of a line even when an API error echoes one", () => {
    // The URL shape is the whole point: `bot123...` has no word boundary
    // before the digits, and a \b-anchored pattern silently misses it.
    expect(redact("GET https://api.telegram.org/bot123456789:AAH1234567890abcdefghijklmnopqrstuvw/x"))
      .toBe("GET https://api.telegram.org/bot[REDACTED]/x");
  });

  it("caps one line's detail, so a 120-pack publish cannot eat the budget", () => {
    const out = redact("x".repeat(9000));
    expect(out.length).toBeLessThan(2100);
    expect(out).toContain("+7000 chars");
  });

  it("writes the row and the eviction in ONE batch", async () => {
    const { db, runs } = stubDb();
    await log({ ...ENV, DB: db }, { bot: "coin", level: "INFO", event: "webhook" });
    expect(runs).toHaveLength(2);
    expect(runs[0].sql).toContain("INSERT INTO logs");
    // Same batch, so a row can never be inserted without its budget check --
    // which is how a table quietly grows past the cap.
    expect(runs[1].sql).toContain("DELETE FROM logs");
    expect(runs[1].args[0]).toBe(10 * 1024 * 1024);
  });

  it("counts BYTES, not characters, so non-ASCII cannot overshoot the cap", async () => {
    const { db, runs } = stubDb();
    await log({ ...ENV, DB: db }, { bot: "coin", level: "INFO", event: "e", detail: "سلام" });
    const bytes = runs[0].args[5] as number;
    // 4 Persian characters are 8 UTF-8 bytes; "coin"+"INFO"+"e" adds 9.
    expect(bytes).toBe(17);
  });

  it("sends an ERROR to the log channel and an INFO nowhere near it", async () => {
    const calls = stubApi();
    await log({ ...ENV, LOG_CHAT_ID: "-1001" },
              { bot: "general", level: "INFO", event: "webhook" });
    expect(calls).toHaveLength(0);
    await log({ ...ENV, LOG_CHAT_ID: "-1001" },
              { bot: "general", level: "ERROR", event: "webhook", detail: "boom" });
    expect(calls).toHaveLength(1);
    expect(calls[0].body.chat_id).toBe("-1001");
    expect(String(calls[0].body.text)).toBe("[general] ERROR webhook\nboom");
  });

  it("posts a bot's own lines with that bot's token", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      seen.push(String(url));
      return new Response(JSON.stringify({ ok: true, result: { message_id: 1, chat: { id: 1 } } }),
                          { headers: { "Content-Type": "application/json" } });
    });
    await log({ ...ENV, LOG_CHAT_ID: "-1001" }, { bot: "coin", level: "ERROR", event: "x" });
    // The poster must match the [coin] tag the line claims.
    expect(seen[0]).toContain("/bot222:coin/");
  });

  it("never rejects, so a dead sink cannot drop an update", async () => {
    stubApi();     // both sinks must be attempted; neither may reach a network
    const exploding = {
      prepare: () => { throw new Error("D1 is down"); },
    } as unknown as D1Database;
    await expect(log({ ...ENV, DB: exploding, LOG_CHAT_ID: "-1001" },
                     { bot: "coin", level: "ERROR", event: "x" })).resolves.toBeUndefined();
  });

  it("an unauthorised webhook hit is recorded but NOT broadcast", async () => {
    // This URL is public. Level-based routing would let a scanner turn the log
    // channel into a firehose.
    const calls = stubApi();
    await worker.fetch(webhookReq("/tg/general", "wrong-secret", { update_id: 9 }),
                       { ...ENV, LOG_CHAT_ID: "-1001" }, CTX);
    await settle();
    expect(calls).toHaveLength(0);
  });

  it("a handler failure IS broadcast, because nothing else reports it", async () => {
    const calls: { method: string; body: Record<string, unknown> }[] = [];
    let first = true;
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      const method = String(url).split("/").pop() ?? "";
      if (first && method === "sendMessage") {
        first = false;   // the reply to the admin fails
        return new Response(JSON.stringify({ ok: false, description: "blocked", error_code: 403 }),
                            { headers: { "Content-Type": "application/json" } });
      }
      calls.push({ method, body: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify({ ok: true, result: { message_id: 1, chat: { id: 1 } } }),
                          { headers: { "Content-Type": "application/json" } });
    });
    const res = await worker.fetch(webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET, {
      update_id: 10, message: msgFrom(42, { text: "hi" }),
    }), { ...ENV, LOG_CHAT_ID: "-1001" }, CTX);
    expect(res.status).toBe(200);      // still 200, or Telegram redelivers
    await settle();
    expect(calls).toHaveLength(1);
    expect(calls[0].body.chat_id).toBe("-1001");
    expect(String(calls[0].body.text)).toMatch(/^\[general\] ERROR webhook/);
  });
});

describe("bearer check", () => {
  it("rejects a missing or malformed Authorization header", () => {
    const mk = (h?: Record<string, string>) =>
      new Request("https://w.dev/publish", { method: "POST", headers: h });
    expect(verifyBearer(mk(), "s")).toBe(false);
    expect(verifyBearer(mk({ Authorization: "s" }), "s")).toBe(false);
    expect(verifyBearer(mk({ Authorization: "Bearer wrong" }), "s")).toBe(false);
    expect(verifyBearer(mk({ Authorization: "Bearer s" }), "s")).toBe(true);
  });
});
