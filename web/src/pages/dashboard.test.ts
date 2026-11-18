import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Dashboard, dialsLine } from "./Dashboard";

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
      attention: [], notices: [], cron: [], jobs: [], metrics: {},
      conversations: [{ id: "c1", title: "Trip planning", source: "web", updated_at: "" }],
      artifacts: [{ id: "a1", name: "2026-11-10.md", kind: "news", media_type: "text/markdown", is_today: true }],
    } };
    const html = render();
    expect(html.match(/full-panel/g)).toHaveLength(2);
    expect(html.indexOf(">Attention<")).toBeLessThan(html.indexOf(">Sessions<"));
    expect(html).toMatch(/<a class="kit-card-head" href="\/sessions\/c1"[^>]*>.*<b>Trip planning<\/b>/);
    expect(html).toMatch(/<a class="kit-button kit-button-primary kit-button-sm" href="\/artifacts"[^>]*><span>Open artifact<\/span>/);
    for (const gone of [">Active now<", ">Memory<", ">Artifacts<"]) expect(html).not.toContain(gone);
    // Issue #81: eight rows of north's own "inference call · memory · completed" said nothing.
    expect(html).not.toContain("Latest events");
  });
});
