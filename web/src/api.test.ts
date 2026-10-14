import { afterEach, describe, expect, it, vi } from "vitest";
import { api, post } from "./api";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

afterEach(() => vi.unstubAllGlobals());

describe("a session the server forgot", () => {
  it("is renewed once and the request retried with the new token", async () => {
    const calls: { path: string; csrf: string | null }[] = [];
    let session = "old";
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit = {}) => {
      calls.push({ path, csrf: new Headers(init.headers).get("X-North-CSRF") });
      if (path === "/web/session") { session = "new"; return json(200, { csrf: "fresh" }); }
      return session === "old" ? json(403, { detail: "Invalid credentials." }) : json(200, { ok: true });
    }));

    await expect(post("/web/api/setup", { step: 1 })).resolves.toEqual({ ok: true });
    expect(calls.map(call => call.path)).toEqual(["/web/api/setup", "/web/session", "/web/api/setup"]);
    expect(calls[2].csrf).toBe("fresh");
  });

  it("does not loop when the renewed session is refused too", async () => {
    const fetch = vi.fn(async (path: string) =>
      path === "/web/session" ? json(200, { csrf: "x" }) : json(403, { detail: "Invalid credentials." }));
    vi.stubGlobal("fetch", fetch);

    await expect(api("/web/api/system")).rejects.toThrow("Invalid credentials.");
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
