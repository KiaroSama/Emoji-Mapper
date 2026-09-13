/**
 * What POST /publish does with a body it did not write.
 *
 * The body is unknown JSON from an authenticated caller, and `PublishRequest`
 * is only a compile-time claim about it. Authenticated is not well-formed:
 * `null` threw a TypeError past every handler, a misspelt "genreal" announced
 * through the coin bot anyway, a string `count` reached the HTML Telegram
 * parses, and a 5000-character note left as one message Telegram rejects.
 *
 * Every refusal is asserted together with "and sent nothing while learning it".
 *
 * No network: `fetch` is stubbed, the same rule the rest of the suite follows.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import worker from "../src/index";
import { renderAnnouncement } from "../src/handle";
import type { Env } from "../src/types";

const ENV: Env = {
  GENERAL_BOT_TOKEN: "111:general",
  COIN_BOT_TOKEN: "222:coin",
  GENERAL_WEBHOOK_SECRET: "general-hook-secret",
  COIN_WEBHOOK_SECRET: "coin-hook-secret",
  PUBLISH_SECRET: "publish-bearer",
  ADMIN_USER_IDS: "42",
  PACK_LINKS_CHAT_ID: "@testchannel",
  TELEGRAM_API_BASE: "https://api.telegram.invalid",
};

const SECRETS = [ENV.PUBLISH_SECRET, ENV.GENERAL_BOT_TOKEN, ENV.COIN_BOT_TOKEN,
                 ENV.GENERAL_WEBHOOK_SECRET, ENV.COIN_WEBHOOK_SECRET];

/** `waitUntil` is collected so a test can await the logging a route fired. */
const pending: Promise<unknown>[] = [];
const CTX = {
  waitUntil: (p: Promise<unknown>) => { pending.push(p); },
  passThroughOnException: () => {},
} as unknown as ExecutionContext;
const settle = () => Promise.allSettled(pending.splice(0));

interface ApiCall { url: string; method: string; body: Record<string, unknown> }

/** Records every Bot API call instead of making one. */
function stubApi(): ApiCall[] {
  const calls: ApiCall[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({ url: String(url), method: String(url).split("/").pop() ?? "",
                 body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1, chat: { id: 1 } } }),
                        { headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

/** A /publish request carrying the real bearer and a RAW JSON body. */
function publish(json: string): Request {
  return new Request("https://w.dev/publish", {
    method: "POST",
    headers: { Authorization: `Bearer ${ENV.PUBLISH_SECRET}`,
               "Content-Type": "application/json" },
    body: json,
  });
}

const OK_PACK = { name: "gvcryptoemoji1_by_bot", title: "Coins 1" };
const PACKS = [OK_PACK];

/**
 * What Telegram counts: the visible text after it parses the HTML.
 *
 * Written out here rather than imported, so a mistake in the Worker's own idea
 * of "length" cannot agree with itself and pass. `&amp;` is five source
 * characters and one visible one; `<b>x</b>` is eight and one. Entities decode
 * lt/gt before amp, or `&amp;lt;` would wrongly collapse to `<`.
 */
function parsedLength(html: string): number {
  return html.replace(/<[^>]*>/g, "")
             .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&")
             .length;
}

/** Everything one message must satisfy before it is worth sending. */
function assertSendable(text: string): void {
  expect(parsedLength(text)).toBeLessThanOrEqual(4096);
  // Balanced markup: a cut inside a tag pair is a 400 from Telegram.
  expect((text.match(/<b>/g) ?? []).length).toBe((text.match(/<\/b>/g) ?? []).length);
  // Every "&" we emit opens an entity; a bare one means a cut entity.
  expect(text).not.toMatch(/&(?!(amp|lt|gt);)/);
  // A half surrogate pair is not valid text at all.
  expect(text).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  expect(text).not.toMatch(/(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/);
}

/** A payload that must be refused before anything leaves the Worker. */
async function rejects(json: string): Promise<void> {
  const calls = stubApi();
  const res = await worker.fetch(publish(json), ENV, CTX);
  await settle();
  expect(res.status, `should have refused: ${json.slice(0, 80)}`).toBe(400);
  // Zero side effects. The log channel is a sendMessage too, so one call here
  // would mean an invalid payload still reached Telegram.
  expect(calls).toHaveLength(0);
  const text = await res.text();
  for (const secret of SECRETS) expect(text).not.toContain(secret);
  // A reason, not a stack trace or an internal error class.
  expect(text).not.toMatch(/TypeError|ReferenceError|\bat \w+ \(/);
  expect(text.length).toBeGreaterThan(0);
}

/** A payload that must go through; returns the sendMessage calls it made. */
async function accepted(body: unknown): Promise<ApiCall[]> {
  const calls = stubApi();
  const res = await worker.fetch(publish(JSON.stringify(body)), ENV, CTX);
  await settle();
  expect(res.status).toBe(200);
  return calls.filter((c) => c.method === "sendMessage");
}

beforeEach(() => { vi.unstubAllGlobals(); pending.length = 0; });

describe("F14 - /publish checks the JSON before it reads it", () => {
  it("refuses a null body instead of throwing past every handler", async () => {
    // `body.packs` on null is a TypeError and onPublish has no catch around
    // it, so the request died as an unhandled exception rather than a 400.
    await rejects("null");
  });

  it("refuses scalars and arrays", async () => {
    for (const json of ["123", '"packs"', "true",
                        "[]", '[{"name":"gvcryptoemoji1_by_bot"}]']) {
      await rejects(json);
    }
  });

  it("refuses a missing, empty, non-array or unbounded packs list", async () => {
    await rejects("{}");
    await rejects('{"packs":{}}');
    await rejects('{"packs":[]}');
    await rejects('{"packs":"gvcryptoemoji1_by_bot"}');
    await rejects(JSON.stringify({
      packs: Array.from({ length: 501 }, (_, i) => ({ name: `set_${i}` })),
    }));
  });

  it("refuses a null or non-object pack entry", async () => {
    await rejects('{"packs":[null]}');
    await rejects('{"packs":["gvcryptoemoji1_by_bot"]}');
    await rejects('{"packs":[[]]}');
    await rejects(JSON.stringify({ packs: [OK_PACK, null] }));
  });

  it("refuses a name that is not a Telegram set name", async () => {
    // It goes into a public t.me/addemoji link; anything else is not one.
    for (const name of ["../../evil", "", "a".repeat(65), "has space",
                        "dash-set", "sl/ash"]) {
      await rejects(JSON.stringify({ packs: [{ name }] }));
    }
  });

  it("refuses a non-string title or format", async () => {
    // `(123).slice` threw inside the renderer -- after the handler had already
    // decided the request was fine.
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", title: 123 }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", title: null }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", title: { a: 1 } }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", title: ["x"] }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", format: 7 }] }));
    await rejects(JSON.stringify({
      packs: [{ name: "ok_set", title: "t".repeat(4097) }],
    }));
  });

  it("refuses a count that is not a non-negative integer", async () => {
    // A string count landed straight in the HTML as ` - <b>forged count</b>`.
    await rejects(JSON.stringify({
      packs: [{ name: "ok_set", count: "<b>forged count</b>" }],
    }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", count: -1 }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", count: 1.5 }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", count: null }] }));
    await rejects(JSON.stringify({ packs: [{ name: "ok_set", count: true }] }));
    // JSON.stringify turns both of these into `null`, so they are written raw.
    await rejects('{"packs":[{"name":"ok_set","count":1e400}]}');   // Infinity
    await rejects('{"packs":[{"name":"ok_set","count":1e30}]}');    // past safe ints
  });

  it("refuses a misspelt bot instead of posting from the other one", async () => {
    // "genreal" fell through `body.bot === "general" ? ... : "coin"`, so a
    // general pack was announced by the coin bot with no complaint.
    await rejects(JSON.stringify({ bot: "genreal", packs: PACKS }));
    await rejects(JSON.stringify({ bot: "", packs: PACKS }));
    await rejects(JSON.stringify({ bot: null, packs: PACKS }));
    await rejects(JSON.stringify({ bot: ["coin"], packs: PACKS }));
    await rejects(JSON.stringify({ bot: "COIN", packs: PACKS }));
  });

  it("refuses an unknown style instead of silently rendering cards", async () => {
    await rejects(JSON.stringify({ style: "bold", packs: PACKS }));
    await rejects(JSON.stringify({ style: 1, packs: PACKS }));
    await rejects(JSON.stringify({ style: null, packs: PACKS }));
  });

  it("refuses a non-string note", async () => {
    await rejects(JSON.stringify({ note: 42, packs: PACKS }));
    await rejects(JSON.stringify({ note: { text: "x" }, packs: PACKS }));
  });
});

describe("F14 - what a valid publish still does", () => {
  it("keeps the documented default: an omitted bot posts as the coin bot", async () => {
    const sends = await accepted({ packs: PACKS });
    expect(sends).toHaveLength(1);
    expect(sends[0].url).toContain(`/bot${ENV.COIN_BOT_TOKEN}/`);
    expect(sends[0].body.chat_id).toBe("@testchannel");
  });

  it("routes general and coin to their own tokens", async () => {
    expect((await accepted({ bot: "general", packs: PACKS }))[0].url)
      .toContain(`/bot${ENV.GENERAL_BOT_TOKEN}/`);
    expect((await accepted({ bot: "coin", packs: PACKS }))[0].url)
      .toContain(`/bot${ENV.COIN_BOT_TOKEN}/`);
  });

  it("accepts the exact shape the Python publishers send", async () => {
    // build_pack / build_collection / coins -- name+title, an explicit bot,
    // and for the coin family a note and style="list".
    const sends = await accepted({
      bot: "coin", style: "list",
      note: "\u{1F4E6} @GodVerify Crypto Emoji — all packs:",
      packs: [{ name: "gvcryptoemoji1_by_bot", title: "1", count: 200 },
              { name: "gvcryptoemoji2_by_bot", title: "2" }],
    });
    expect(sends).toHaveLength(1);
    const text = String(sends[0].body.text);
    expect(text).toContain("t.me/addemoji/gvcryptoemoji1_by_bot");
    expect(text).toContain("t.me/addemoji/gvcryptoemoji2_by_bot");
    expect(text).not.toContain("✅");     // list style has no card ticks
    assertSendable(text);
  });

  it("escapes hostile markup at the rendering boundary", async () => {
    const sends = await accepted({
      packs: [{ name: "ok_set", title: '<b>x</b>&"<script>', count: 200 }],
      note: "<i>note</i> & more",
    });
    const text = String(sends[0].body.text);
    expect(text).toContain("&lt;b&gt;x&lt;/b&gt;&amp;");
    expect(text).toContain("&lt;i&gt;note&lt;/i&gt; &amp; more");
    expect(text).not.toContain("<script>");
    assertSendable(text);
  });
});

describe("F15 - an oversized note cannot escape the chunker", () => {
  it("no longer emits a 5000-character message", () => {
    // The chunker only ever split BETWEEN blocks, so the note -- one block --
    // went out whole: the two message lengths were [5000, 60].
    const parts = renderAnnouncement({ note: "a".repeat(5000), packs: PACKS });
    for (const p of parts) assertSendable(p);
  });

  it("refuses an oversized note rather than half-delivering it", async () => {
    await rejects(JSON.stringify({ note: "a".repeat(5000), packs: PACKS }));
  });

  it("holds at 4095 and 4096, and refuses 4097", async () => {
    for (const n of [4095, 4096]) {
      const parts = renderAnnouncement({ note: "a".repeat(n), packs: PACKS });
      expect(parsedLength(parts[0])).toBe(n);   // the note keeps its own message
      for (const p of parts) assertSendable(p);
    }
    await rejects(JSON.stringify({ note: "a".repeat(4097), packs: PACKS }));
    // And called directly the renderer still cannot emit an over-limit message.
    for (const p of renderAnnouncement({ note: "a".repeat(4097), packs: PACKS })) {
      assertSendable(p);
    }
  });

  it("bounds what Telegram counts, not the escaped source", async () => {
    // "&" is one visible character and five source ones. 4096 of them is a
    // legal 20480-character message; a source-length bound would refuse it.
    const sends = await accepted({ note: "&".repeat(4096), packs: PACKS });
    expect(sends.length).toBeGreaterThan(1);
    expect(String(sends[0].body.text)).toBe("&amp;".repeat(4096));
    for (const c of sends) assertSendable(String(c.body.text));
    await rejects(JSON.stringify({ note: "&".repeat(4097), packs: PACKS }));
  });

  it("keeps a long Persian note whole", () => {
    // Persian is BMP -- one UTF-16 unit per character -- and right-to-left.
    // The bound must not reorder it, split a character, or mangle the run.
    const line = "سلام دنیا ";                        // 10 units
    const note = line.repeat(409) + "سلام";            // 4094 units
    expect(note.length).toBe(4094);
    const parts = renderAnnouncement({ note, packs: PACKS });
    expect(parts[0]).toBe(note);
    expect(parsedLength(parts[0])).toBe(4094);
    for (const p of parts) assertSendable(p);
  });

  it("refuses an oversized Persian note", async () => {
    await rejects(JSON.stringify({ note: "سلام دنیا ".repeat(500), packs: PACKS }));
  });

  it("never cuts an emoji in half", async () => {
    // Telegram counts UTF-16 units, so an astral emoji costs 2 and a cut at
    // 4096 lands inside one -- emitting a lone surrogate.
    const note = "a".repeat(4095) + "\u{1F600}".repeat(10);   // 4115 units
    const first = renderAnnouncement({ note, packs: PACKS })[0];
    expect(first).toBe("a".repeat(4095));      // dropped whole, not halved
    assertSendable(first);
    const full = "\u{1F600}".repeat(2048);     // exactly 4096 units
    expect(renderAnnouncement({ note: full, packs: PACKS })[0]).toBe(full);
    await rejects(JSON.stringify({ note: "\u{1F600}".repeat(2049), packs: PACKS }));
  });

  it("keeps every pack link intact across a whole family", async () => {
    const packs = Array.from({ length: 500 }, (_, i) => ({
      name: `gvcryptoemoji${i + 1}_by_GodVerifyCoinEmojiMapperbot`,
      title: `Crypto & pack <${i + 1}>`,
      count: 200,
    }));
    const sends = await accepted({ bot: "coin", note: "all packs:", packs });
    expect(sends.length).toBeGreaterThan(1);
    for (const c of sends) {
      const text = String(c.body.text);
      assertSendable(text);
      // A pack entry is never split away from its own link.
      expect((text.match(/t\.me\/addemoji\//g) ?? []).length)
        .toBe((text.match(/✅/g) ?? []).length);
    }
    const joined = sends.map((c) => String(c.body.text)).join("\n");
    for (const p of packs) expect(joined).toContain(`https://t.me/addemoji/${p.name}`);
  });
});
