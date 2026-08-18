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
                 { update_id: 1, message: msgFrom(42) }), ENV);
    expect(res.status).toBe(401);
    expect(calls).toHaveLength(0);
  });
});

describe("only admins get answered", () => {
  it("denies a stranger in private, and sends nothing else", async () => {
    const calls = stubApi();
    const res = await worker.fetch(
      webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET,
                 { update_id: 2, message: msgFrom(999) }), ENV);
    expect(res.status).toBe(200);          // 200, or Telegram redelivers forever
    expect(calls).toHaveLength(1);
    expect(String(calls[0].body.text)).toContain("not on its access list");
  });

  it("stays completely silent to a stranger in a group", async () => {
    const calls = stubApi();
    await worker.fetch(webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET, {
      update_id: 3,
      message: msgFrom(999, { chat: { id: -100, type: "supergroup", title: "G" } }),
    }), ENV);
    // Answering here would make the bot a spam vector in any group it is in.
    expect(calls).toHaveLength(0);
  });

  it("answers an admin", async () => {
    const calls = stubApi();
    await worker.fetch(webhookReq("/tg/general", ENV.GENERAL_WEBHOOK_SECRET, {
      update_id: 4,
      message: msgFrom(42, {
        text: "hi",
        entities: [{ type: "custom_emoji", offset: 0, length: 2, custom_emoji_id: "111111111" }],
      }),
    }), ENV);
    expect(calls).toHaveLength(1);
    expect(String(calls[0].body.text)).toContain("111111111");
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
    packs: [{ name: "cryptoemoji1_by_bot", title: "Coins 1", count: 200 }],
  };

  it("refuses without the bearer", async () => {
    const calls = stubApi();
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST", body: JSON.stringify(body),
    }), ENV);
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
    }), ENV);
    expect(res.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("posts the pack link to the configured channel", async () => {
    const calls = stubApi();
    const res = await worker.fetch(new Request("https://w.dev/publish", {
      method: "POST",
      headers: { Authorization: `Bearer ${ENV.PUBLISH_SECRET}` },
      body: JSON.stringify(body),
    }), ENV);
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].body.chat_id).toBe("@testchannel");
    expect(String(calls[0].body.text)).toContain("t.me/addemoji/cryptoemoji1_by_bot");
  });

  it("escapes a title so it cannot inject markup", () => {
    const text = renderAnnouncement({ packs: [{ name: "ok_set", title: "<b>x</b>&" }] });
    expect(text).toContain("&lt;b&gt;x&lt;/b&gt;&amp;");
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
