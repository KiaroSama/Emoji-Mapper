/** Validate the external JSON envelope before dereferencing it or sending. */
import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/types";

const env: Env = {
  GENERAL_BOT_TOKEN: "111:fixture", COIN_BOT_TOKEN: "222:fixture",
  GENERAL_WEBHOOK_SECRET: "general-fixture", COIN_WEBHOOK_SECRET: "coin-fixture",
  PUBLISH_SECRET: "fixture", ADMIN_USER_IDS: "42", PACK_LINKS_CHAT_ID: "@fixture",
  TELEGRAM_API_BASE: "https://telegram.invalid",
};
const pending: Promise<unknown>[] = [];
const ctx = { waitUntil: (p: Promise<unknown>) => pending.push(p) } as unknown as ExecutionContext;
function req(bot: string, body: string, secret = `${bot}-fixture`): Request {
  return new Request(`https://worker.invalid/tg/${bot}`, {
    method: "POST", body,
    headers: { "Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret },
  });
}
afterEach(async () => { await Promise.allSettled(pending.splice(0)); vi.unstubAllGlobals(); });

describe.each(["general", "coin"])("%s webhook envelope", bot => {
  it.each(["null", "[]", '"string"', "1", "true", "{}", '{"update_id":null}',
    '{"update_id":true}', '{"update_id":"1"}', '{"update_id":1.5}',
    '{"update_id":-1}', '{"update_id":9007199254740992}', '{"update_id":1e309}', "{"])
    ("refuses %s without any Telegram side effects", async body => {
      const fetch = vi.fn(() => { throw new Error("no outbound calls expected"); });
      vi.stubGlobal("fetch", fetch);
      const response = await worker.fetch(req(bot, body), env, ctx);
      expect(response.status).toBe(400);
      expect(await response.text()).toBe("bad request");
      await Promise.allSettled(pending.splice(0));
      expect(fetch).not.toHaveBeenCalled();
    });

  it("accepts an unknown future update type with a valid identifier", async () => {
    const fetch = vi.fn(() => { throw new Error("no outbound calls expected"); });
    vi.stubGlobal("fetch", fetch);
    const response = await worker.fetch(req(bot, '{"update_id":17,"future_event":{"field":1}}'), env, ctx);
    expect(response.status).toBe(200);
    await Promise.allSettled(pending.splice(0));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("authenticates before parsing an invalid body", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("no outbound calls expected"); }));
    const response = await worker.fetch(req(bot, "null", "wrong-secret"), env, ctx);
    expect(response.status).toBe(401);
  });
});
