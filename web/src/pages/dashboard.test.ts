import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Dashboard, dialsLine, problems, systemRows, visitSince } from "./Dashboard";

// What each endpoint returns in the render tests; a path left out is still loading.
const served = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string | null) => ({ data: (path && served.data[path]) ?? null, loading: !(path && path in served.data), error: "", reload: async () => {} }),
}));

const render = () => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(Dashboard)));
const HOUR = 3_600_000;
const ago = (hours: number) => new Date(Date.now() - hours * HOUR).toISOString();

describe("the System facts", () => {
  it("names the approval mode only: models and power are on the Settings page (#80)", () => {
    expect(dialsLine({ status: "online", autonomy: "ask" })).toBe("Autonomy ask");
  });

  it("lists each model, each wait and each browser profile, with its state", () => {
    const rows = systemRows({
      status: "online", autonomy: "autonomous", version: "1.21.0",
      models: [{ name: "Mistral", state: "ready", detail: "" }, { name: "Groq", state: "failing", detail: "key rejected" }],
      waiting: [{ model: "mistral:flash", until: 2e9, reason: "rate limited" }],
      browser: [{ name: "sjsu.edu", connected: true }, { name: "Personal", connected: false }],
    });
    expect(rows.map(row => [row.title, row.state, row.tone])).toEqual([
      ["Mistral", "ready", "good"], ["Groq", "failing", "bad"], ["mistral:flash", "waiting", "warn"],
      ["Browser: sjsu.edu", "connected", "good"], ["Browser: Personal", "not connected", "neutral"],
    ]);
  });
});

describe("Is anything broken?", () => {
  const system = {
    status: "online", autonomy: "ask",
    models: [{ name: "Groq", state: "ready", detail: "" }, { name: "Mistral", state: "failing", detail: "key rejected" }],
    waiting: [{ model: "gpt-6.1-sol", until: 2e9, reason: "rate limited (429)" }],
    browser: [{ name: "Personal", connected: false }],
  };

  it("lists only what is wrong: a failing model, a wait, an agent that cannot run, a flow that did not finish", () => {
    const runs = [
      { run_id: "2", flow: "nightly-check", status: "cancelled", started_at: ago(1) },
      { run_id: "1", flow: "nightly-check", status: "completed", started_at: ago(30) },
      { run_id: "3", flow: "daily-news", status: "completed", started_at: ago(2) },
    ];
    const list = problems(system, "online", [{ name: "codex", ready: true, detail: "" }, { name: "claude", ready: false, detail: "not logged in" }], runs);
    expect(list.map(problem => problem.title)).toEqual(["Mistral", "gpt-6.1-sol", "Coding agent claude", "Flow nightly-check"]);
    // A ready model and an unconnected browser profile are not problems; each lives on Settings.
    expect(list.map(problem => problem.title)).not.toContain("Groq");
    expect(list.find(problem => problem.key === "f:nightly-check")?.to).toBe("/flows?open=nightly-check");
  });

  it("judges a flow by its newest run only", () => {
    const runs = [
      { run_id: "2", flow: "nightly-check", status: "completed", started_at: ago(1) },
      { run_id: "1", flow: "nightly-check", status: "cancelled", started_at: ago(30) },
    ];
    expect(problems(undefined, "online", [], runs)).toEqual([]);
  });

  it("says when north itself fails a check", () => {
    expect(problems(undefined, "degraded", [], [])[0].to).toBe("/system");
  });
});

describe("What changed? counts from your last time away", () => {
  const now = 1_800_000_000_000;
  it("looks back one day on a first visit", () => {
    expect(visitSince(now, 0, 0)).toEqual({ since: now - 24 * HOUR, lastSeen: now });
  });
  it("keeps the same point while you stay, so a reload does not empty it", () => {
    expect(visitSince(now, now - 5 * 60_000, now - 9 * HOUR).since).toBe(now - 9 * HOUR);
  });
  it("starts from when you left after 30 minutes away", () => {
    expect(visitSince(now, now - 2 * HOUR, now - 9 * HOUR).since).toBe(now - 2 * HOUR);
  });
});

describe("the Home page", () => {
  it("keeps its header and Refresh while it loads", () => {
    served.data = {};
    const html = render();
    expect(html).toContain("<h1>Home</h1>");
    expect(html).toContain("<span>Refresh</span>");
    expect(html).toContain("kit-loading");
  });

  it("asks three questions, in order, and repeats no other page", () => {
    served.data = {
      "/web/api/dashboard": { system: { status: "online", autonomy: "ask" }, attention: [], notices: [], metrics: {}, conversations: [] },
      "/web/api/approvals": [
        { id: "card-1", title: "Send 10 requests?", agent: "outreach", status: "pending", blocking: false, created_at: ago(30) },
        { id: "card-2", title: "Old decision", agent: "north", status: "approved", blocking: false, created_at: ago(40) },
      ],
      "/web/api/notices": [{ id: "n1", title: "Briefing ready", message: "", created_at: ago(1) }],
      "/web/api/flow-runs?limit=100": [
        { run_id: "r1", flow: "daily-photography-flashcard", status: "paused", started_at: ago(11), updated_at: ago(11), error: "", current_step: 1, total_steps: 1 },
        { run_id: "r2", flow: "daily-news", status: "completed", started_at: ago(2), updated_at: ago(2), error: "", current_step: 1, total_steps: 1 },
      ],
      "/web/api/conversations": [{ id: "c1", title: "Trip planning", source: "web", updated_at: ago(1) }],
      "/web/api/artifacts": [],
      "/web/api/setup/coding-agents": [],
    };
    const html = render();
    const order = [">What needs me?<", ">What changed?<", ">Is anything broken?<"].map(title => html.indexOf(title));
    expect(order.every(at => at >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    // Waiting items, oldest first, each linking to itself in the Inbox; a decided card is not one.
    expect(html.indexOf("Send 10 requests?")).toBeLessThan(html.indexOf("daily-photography-flashcard paused"));
    expect(html).toContain('href="/inbox?focus=card-1"');
    expect(html).toContain('href="/inbox?focus=r1"');
    expect(html).not.toContain("Old decision");
    expect(html).toContain("waiting 30 h");
    expect(html).toContain('href="/sessions/c1"');
    // The tiles that copied other pages are gone: health, version, autonomy, cost and each run.
    for (const gone of [">System<", ">Background<", ">Attention<", "Model cost", "Autonomy"]) expect(html).not.toContain(gone);
    expect(html).toContain("Nothing is broken.");
  });

  it("puts Set up North first, inside the grid, until setup is done", () => {
    served.data = {
      "/web/api/setup": { status: "in_progress" },
      "/web/api/dashboard": { system: { status: "online", autonomy: "ask" }, attention: [], notices: [], metrics: {}, conversations: [] },
    };
    const html = render();
    const grid = html.indexOf("cockpit-grid");
    expect(grid).toBeGreaterThan(-1);
    expect(html.indexOf("Set up North")).toBeGreaterThan(grid);
    expect(html.indexOf("Set up North")).toBeLessThan(html.indexOf(">What needs me?<"));
  });
});
