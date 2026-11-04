import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, bootstrapSession, post } from "./api";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

const page = (search = "") => ({ location: { search, pathname: "/app/", hash: "#/" }, history: { replaceState: vi.fn() } });

beforeEach(() => vi.stubGlobal("window", page()));
afterEach(() => vi.unstubAllGlobals());

describe("signing a browser in", () => {
  it("sends the one-time token from `north web` once and clears it from the address", async () => {
    const window = page("?pair=one-time");
    vi.stubGlobal("window", window);
    const fetch = vi.fn(async (_path: string, _init: RequestInit) => json(200, { csrf: "c" }));
    vi.stubGlobal("fetch", fetch);

    await bootstrapSession();

    const [, init] = fetch.mock.calls[0]!;
    expect(JSON.parse(String(init.body))).toEqual({ pair: "one-time" });
    expect(window.history.replaceState).toHaveBeenCalledWith(null, "", "/app/#/");
  });

  it("says why a browser that is not signed in gets nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      json(401, { detail: "This browser is not signed in to north. Open the dashboard with: north web" })));

    await expect(bootstrapSession()).rejects.toThrow("Open the dashboard with: north web");
  });
});

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

describe("a request the server refuses", () => {
  it("says what was wrong with it in words, not [object Object]", async () => {
    // Issue #76, M11: a prompt over 32,768 characters got FastAPI's list of field errors back.
    vi.stubGlobal("fetch", vi.fn(async () => json(422, {
      detail: [{ type: "string_too_long", loc: ["body", "prompt"], msg: "String should have at most 32768 characters", input: "x" }],
    })));

    await expect(post("/web/api/conversations/c1/turns", { prompt: "x" })).rejects.toThrow(
      "prompt: String should have at most 32768 characters",
    );
  });
});
