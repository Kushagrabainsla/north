import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Dashboard, dialsLine, memoryLine } from "./Dashboard";

// What each endpoint returns in the render tests; a path left out is still loading.
const served = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: served.data[path] ?? null, loading: !(path in served.data), error: "", reload: async () => {} }),
}));

const render = () => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(Dashboard)));

// Issue #76, P12 and P14: the System tile said "Power cruise · ask" while a pinned model decided and
// Settings said power was not in use; the Memory tile said "4 context documents" over an empty memory.
describe("the dashboard's tiles say what is so", () => {
  it("names the pinned model, not power, under manual routing", () => {
    const pinned = { status: "online", power: "cruise", autonomy: "ask", routing: "manual", model: "anthropic:claude-haiku-5-5" };
    expect(dialsLine(pinned)).toBe("Model claude-haiku-5-5 (pinned) · ask");
    expect(dialsLine({ ...pinned, routing: "auto", model: null })).toBe("Power cruise · ask");
  });

  it("counts what memory holds", () => {
    expect(memoryLine({ facts: 0, documents: 0, of_documents: 2 })).toEqual({
      count: 0,
      label: "facts about you",
      note: "No north stars or judgement rules written yet.",
    });
    expect(memoryLine({ facts: 1, documents: 1, of_documents: 2 }).note).toBe(
      "1 of 2 documents written: north stars and judgement rules.",
    );
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

  it("shows sessions as cards that open them, and lists as the kit's rows", () => {
    served.data = { "/web/api/dashboard": {
      system: { status: "online", power: "cruise", autonomy: "ask" },
      attention: [], notices: [], cron: [], jobs: [], activity: [], metrics: {},
      active_tasks: [{ task_id: "task-123456789", status: "running", created_at: "" }],
      conversations: [{ id: "c1", title: "Trip planning", source: "web", updated_at: "" }],
      artifacts: [{ id: "a1", name: "2026-11-10.md", kind: "news", media_type: "text/markdown", is_today: true }],
    } };
    const html = render();
    expect(html).toMatch(/<a class="kit-card-head" href="\/sessions\/c1"[^>]*>.*<b>Trip planning<\/b>/);
    expect(html).toMatch(/class="kit-row".*<b>task-1234567<\/b>.*kit-status kit-tone-busy">running/);
    expect(html).toMatch(/<a class="kit-button kit-button-primary kit-button-sm" href="\/artifacts"[^>]*><span>Open artifact<\/span>/);
  });
});
