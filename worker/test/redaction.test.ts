/**
 * R09: a bot token must not leave through an error path.
 *
 * Every Bot API request URL embeds the token, and a transport failure names the
 * URL it was attempting. That message travelled out of `Telegram.call`, through
 * the publish handler's catch, into the HTTP 502 body AND into every log sink --
 * so a caller holding only the publish bearer could be handed the bot token.
 *
 * No real credential is involved here: the "token" below is a synthetic string
 * of the right shape, and the assertions are that it appears NOWHERE in what a
 * caller or a log can see.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { errText, redact, secretsOf } from "../src/redact";
import { formatLine, redact as logRedact } from "../src/logging";
import { BotApiError, Telegram } from "../src/telegram";

// Synthetic, and shaped like the real thing: digits, colon, 35 chars.
const TOKEN = "123456789:SYNTHETICxxxxxxxxxxxxxxxxxxxxxxxx";
const PUBLISH = "synthetic-publish-bearer-value";
const ENV = {
  GENERAL_BOT_TOKEN: TOKEN,
  COIN_BOT_TOKEN: "987654321:OTHERyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
  PUBLISH_SECRET: PUBLISH,
  GENERAL_WEBHOOK_SECRET: "hook-secret-general-value",
  COIN_WEBHOOK_SECRET: "hook-secret-coin-value",
} as never;

describe("redact", () => {
  it("masks a bot URL whatever the token looks like", () => {
    const text = `fetch failed: POST https://api.telegram.org/bot${TOKEN}/sendMessage`;
    const out = redact(text);
    expect(out).not.toContain(TOKEN);
    expect(out).toContain("/bot[REDACTED]/sendMessage");
  });

  it("masks a bare token even outside a URL", () => {
    expect(redact(`token=${TOKEN} rejected`)).not.toContain(TOKEN);
  });

  it("masks a configured secret that has no recognisable shape", () => {
    // PUBLISH_SECRET is arbitrary text; only the env listing can catch it.
    expect(redact(`bearer ${PUBLISH} refused`, ENV)).not.toContain(PUBLISH);
  });

  it("leaves ordinary text alone", () => {
    const msg = "sendMessage failed (400): chat not found";
    expect(redact(msg, ENV)).toBe(msg);
  });

  it("never throws on odd input", () => {
    expect(redact(undefined as never)).toBe("");
    expect(redact(null as never)).toBe("");
  });

  it("lists only credential env values, and ignores short ones", () => {
    expect(secretsOf(ENV)).toContain(TOKEN);
    expect(secretsOf({ PUBLISH_SECRET: "short" } as never)).toEqual([]);
    expect(secretsOf(undefined)).toEqual([]);
  });
});

describe("errText", () => {
  it("flattens a cause chain and redacts every link of it", () => {
    const inner = new Error(`connect ECONNREFUSED https://api.telegram.org/bot${TOKEN}/x`);
    const outer = new Error("publish failed", { cause: inner });
    const out = errText(outer, ENV);
    expect(out).not.toContain(TOKEN);
    expect(out).toContain("publish failed");
    expect(out).toContain("[REDACTED]");
  });

  it("does not spin on a cyclic cause", () => {
    const a = new Error("a") as Error & { cause?: unknown };
    const b = new Error("b", { cause: a }) as Error & { cause?: unknown };
    a.cause = b;
    expect(() => errText(a, ENV)).not.toThrow();
  });
});

/**
 * The error a call threw, typed as an Error.
 *
 * `promise.catch((e) => e as T)` widens the result to `T | <resolved type>`,
 * which typechecks locally only until something reads `.message` on it -- the
 * shape of the CI failure that caught this file.
 */
async function thrownBy(run: () => Promise<unknown>): Promise<Error> {
  try {
    await run();
  } catch (err) {
    return err as Error;
  }
  throw new Error("expected the call to throw, and it resolved");
}

describe("Telegram.call", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("does not put the request URL into a transport error", async () => {
    vi.stubGlobal("fetch", async (url: string) => {
      throw new TypeError(`fetch failed: ${url}`);
    });
    const err = await thrownBy(() => new Telegram(TOKEN).sendMessage(1, "hi"));
    expect(err).toBeInstanceOf(BotApiError);
    expect(err.message).not.toContain(TOKEN);
    expect(err.message).toContain("sendMessage");
  });

  it("redacts an upstream description that echoes a secret back", async () => {
    vi.stubGlobal("fetch", async () => new Response(
      JSON.stringify({ ok: false, description: `bad token ${TOKEN}`, error_code: 401 }),
      { status: 401, headers: { "Content-Type": "application/json" } }));
    const err = await thrownBy(() => new Telegram(TOKEN).sendMessage(1, "hi"));
    expect(err.message).not.toContain(TOKEN);
    expect((err as BotApiError).code).toBe(401);
  });

  it("still reports a non-JSON reply usefully", async () => {
    vi.stubGlobal("fetch", async () => new Response("<html>502</html>",
                                                    { status: 502 }));
    const err = await thrownBy(() => new Telegram(TOKEN).sendMessage(1, "hi"));
    expect(err.message).toContain("non-JSON");
    expect(err.message).not.toContain(TOKEN);
  });

  it("a successful call is unaffected", async () => {
    vi.stubGlobal("fetch", async () => new Response(
      JSON.stringify({ ok: true, result: { message_id: 7, chat: { id: 1 } } }),
      { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(new Telegram(TOKEN).sendMessage(1, "hi"))
      .resolves.toMatchObject({ message_id: 7 });
  });
});

describe("log sinks", () => {
  it("formatLine masks a configured secret in the detail", () => {
    const line = formatLine(
      { bot: "general", level: "ERROR", event: "publish",
        detail: `failed: /bot${TOKEN}/sendMessage and bearer ${PUBLISH}` } as never,
      0, 0, ENV);
    expect(line).not.toContain(TOKEN);
    expect(line).not.toContain(PUBLISH);
  });

  it("the logging redact still caps length after sanitising", () => {
    const long = "x".repeat(5000);
    const out = logRedact(long, ENV);
    expect(out.length).toBeLessThan(long.length);
    expect(out).toContain("chars)");
  });
});
