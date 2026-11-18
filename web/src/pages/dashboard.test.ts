import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Dashboard, dialsLine, systemRows } from "./Dashboard";

// What each endpoint returns in the render tests; a path left out is still loading.
const served = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: served.data[path] ?? null, loading: !(path in served.data), error: "", reload: async () => {} }),
}));

const render = () => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(Dashboard)));

// Issue #76, P14: the System tile said "Power cruise · ask" while a pinned model decided and
// Settings said power was not in use.
describe("the dashboard's tiles say what is so", () => {
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
    expect(rows[2].detail).toMatch(/^until .* · rate limited$/);
  });
});

describe("the dashboard page", () => {
  it("keeps its header and Refresh while it loads", () => {
    served.data = {};
    const html = render();
    expect(html).toContain("<h1>Everything, at a glance</h1>");
    expect(html).toContain("<span>Refresh</span>");
    expect(html).toContain("kit-loading");
  });

  it("gives Attention and Sessions the whole width, and has no Active now, Memory or Artifacts tile", () => {
    served.data = { "/web/api/dashboard": {
      system: { status: "online", autonomy: "ask" },
      attention: [], notices: [], metrics: {},
      conversations: [{ id: "c1", title: "Trip planning", source: "web", updated_at: "" }],
      background: [
        { run_id: "r1", flow: "daily-news-briefing", status: "completed", trigger: "schedule", at: "", error: "",
          file_name: "2026-11-18.md", file_id: "f1" },
        { run_id: "r2", flow: "daily-research-briefing", status: "paused", trigger: "schedule", at: "",
          error: "Step 'compile' ran, but twice did not report its result", file_name: "", file_id: "" },
      ],
    } };
    const html = render();
    // One column: Attention, Sessions, one System panel (usage inside it), then Background.
    expect(html.match(/full-panel/g)).toHaveLength(4);
    const order = [">Attention<", ">Sessions<", ">System<", ">Background<"].map(title => html.indexOf(title));
    expect(order.every(at => at >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html).not.toContain(">Usage<");
    expect(html).toContain("Model cost, last 7 days");
    expect(html).toMatch(/<a class="kit-card-head" href="\/sessions\/c1"[^>]*>.*<b>Trip planning<\/b>/);
    // What north did while you were away, in place of a tile for one person's briefing flows.
    expect(html).toContain(">Background<");
    expect(html).toContain('href="/web/api/artifacts/f1/file"');
    expect(html).toContain("twice did not report its result");
    for (const old of ["Today&#x27;s briefing", "Latest briefing"]) expect(html).not.toContain(old);
    for (const gone of [">Active now<", ">Memory<", ">Artifacts<", ">Flows<"]) expect(html).not.toContain(gone);
    // Issue #81: eight rows of north's own "inference call · memory · completed" said nothing.
    expect(html).not.toContain("Latest events");
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
    expect(html.indexOf("Set up North")).toBeLessThan(html.indexOf(">Attention<"));
  });
});
