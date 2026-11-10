// The UI kit: one component per kind of thing, so it looks and behaves the same on every page.
// Before it, delete buttons were not red, errors showed in the success colour, an active skill
// was a warning colour, and two approval cards ordered Approve and Reject differently.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ActionBar, Button, EntityTable, Field, KeyValues, ListRow, Notice, Panel, RelatedLinks, ResourceView, Status, Tabs, confirmDelete, statusTone } from "./kit";

const html = (element: ReturnType<typeof createElement>) =>
  renderToStaticMarkup(createElement(MemoryRouter, null, element));

describe("buttons", () => {
  it("say what they are by variant, and a delete is red", () => {
    expect(html(createElement(Button, { variant: "danger" }, "Delete"))).toContain("kit-button-danger");
    expect(html(createElement(Button, { variant: "primary" }, "Save"))).toContain("kit-button-primary");
    expect(html(createElement(Button, {}, "Cancel"))).toContain("kit-button-secondary");
  });

  it("read their busy label and cannot be pressed while busy", () => {
    const busy = html(createElement(Button, { busy: true, busyLabel: "Saving…" }, "Save"));
    expect(busy).toContain("Saving…");
    expect(busy).toContain("disabled");
  });

  it("become a link when they go somewhere", () => {
    expect(html(createElement(Button, { to: "/skills" }, "Skills"))).toContain('href="/skills"');
  });

  it("sit in one order: the danger action apart at the start, the main action last", () => {
    const bar = html(createElement(ActionBar, { danger: createElement(Button, { variant: "danger" }, "Delete") },
      createElement(Button, {}, "Cancel"), createElement(Button, { variant: "primary" }, "Save")));
    expect(bar.indexOf("Delete")).toBeLessThan(bar.indexOf("Cancel"));
    expect(bar.indexOf("Cancel")).toBeLessThan(bar.indexOf("Save"));
  });
});

describe("a delete always asks the same way", () => {
  it("names what goes, and its button says so in red", async () => {
    const confirm = vi.fn().mockResolvedValue(true);
    const dialog = { confirm, prompt: vi.fn(), alert: vi.fn() };
    await expect(confirmDelete(dialog, { noun: "skill", name: "daily-digest" })).resolves.toBe(true);
    expect(confirm).toHaveBeenCalledWith("This cannot be undone.", { title: 'Delete skill "daily-digest"?', confirmLabel: "Delete skill", danger: true });
  });
});

describe("a state's colour is set by its word, the same everywhere", () => {
  it("does not show running as done, or an active skill as a warning", () => {
    expect(statusTone("running")).toBe("busy");
    expect(statusTone("completed")).toBe("good");
    expect(statusTone("active")).toBe("good");
    expect(statusTone("failed")).toBe("bad");
    expect(statusTone("paused")).toBe("warn");
    expect(statusTone("something-new")).toBe("neutral");
  });

  it("reads the word without underscores", () => {
    expect(html(createElement(Status, { value: "needs_attention" }))).toContain(">needs attention<");
  });
});

describe("messages about the page", () => {
  it("show a failure as an error, never in the success colour", () => {
    const error = html(createElement(Notice, { tone: "error", children: "Could not save" }));
    expect(error).toContain("kit-notice-error");
    expect(error).toContain('role="alert"');
  });

  it("show loading, a failure and an empty list the same way on every page", () => {
    const body = (resource: { data: string[] | null; loading: boolean; error?: string }) =>
      html(createElement(ResourceView<string[]>, { resource, isEmpty: data => data.length === 0, empty: "No skills yet.", children: data => data.join(",") }));
    expect(body({ data: null, loading: true })).toContain("kit-loading-inline");
    expect(body({ data: null, loading: false, error: "Server down" })).toContain("kit-notice-error");
    expect(body({ data: [], loading: false })).toContain("No skills yet.");
    expect(body({ data: ["a", "b"], loading: false })).toContain("a,b");
  });
});

describe("lists and tabs", () => {
  it("draw a table with its column titles, a row per thing, and opens a row by link", () => {
    const table = html(createElement(EntityTable<{ name: string }>, {
      label: "Skills", columns: [{ key: "name", label: "Skill" }], rows: [{ name: "digest" }],
      rowKey: row => row.name, cell: row => row.name, to: row => `/skills/${row.name}`,
    }));
    expect(table).toContain(">Skill<");
    expect(table).toContain('href="/skills/digest"');
  });

  it("say which tab is chosen", () => {
    const tabs = html(createElement(Tabs<"a" | "b">, { label: "View", value: "b", onChange: () => undefined, items: [{ value: "a", label: "All" }, { value: "b", label: "Mine", count: 2 }] }));
    expect(tabs).toContain('aria-selected="true"');
    expect(tabs).toContain("kit-tab-active");
  });
});

describe("fields", () => {
  it("tie the label to a single input, so clicking the label focuses it", () => {
    const field = html(createElement(Field, { label: "Name", children: createElement("input", { defaultValue: "" }) }));
    const id = /<label class="kit-field-label" id="[^"]+" for="([^"]+)">Name<\/label>/.exec(field)?.[1];
    expect(id).toBeTruthy();
    expect(field).toContain(`<input id="${id}"`);
    expect(field).not.toContain('role="group"');
  });

  it("name a group of choice buttons instead", () => {
    const field = html(createElement(Field, { label: "Days", children: createElement(Button, { "aria-pressed": true }, "Mon") }));
    expect(field).toContain('role="group"');
    expect(field).not.toContain("<label");
  });
});

describe("rows and facts", () => {
  it("mark the row a link landed on", () => {
    expect(html(createElement(ListRow, { title: "Fact", found: true }))).toContain("kit-row-found");
    expect(html(createElement(ListRow, { title: "Fact" }))).not.toContain("kit-row-found");
  });

  it("list facts as label and value, leaving out empty ones", () => {
    const facts = html(createElement(KeyValues, { items: [["Started", "9:00"], ["Error", ""]] }));
    expect(facts).toContain("<dt>Started</dt><dd>9:00</dd>");
    expect(facts).not.toContain("Error");
  });
});

describe("tabs that cannot be used", () => {
  it("are disabled as a set or one by one", () => {
    const items = [{ value: "a" as const, label: "All" }, { value: "b" as const, label: "Mine", disabled: true }];
    const one = html(createElement(Tabs<"a" | "b">, { label: "View", value: "a", onChange: () => undefined, items }));
    expect(one.match(/disabled=""/g)?.length).toBe(1);
    const all = html(createElement(Tabs<"a" | "b">, { label: "View", value: "a", onChange: () => undefined, items, disabled: true }));
    expect(all.match(/disabled=""/g)?.length).toBe(2);
  });
});

describe("less text", () => {
  const rows = [{ name: "a", state: "active" }, { name: "b", state: "active" }];
  const table = (state: string) => html(createElement(EntityTable<{ name: string; state: string }>, {
    label: "Things", rows: [rows[0], { ...rows[1], state }], rowKey: row => row.name, cell: (row, key) => key === "name" ? row.name : row.state,
    columns: [{ key: "name", label: "Name", lines: 1 }, { key: "state", label: "State", same: (row: { state: string }) => row.state }],
  }));

  it("leaves out a column that says the same on every row", () => {
    expect(table("active")).not.toContain(">State<");
    expect(table("paused")).toContain(">State<");
  });

  it("cuts a column to its lines", () => {
    expect(table("active")).toContain('class="kit-table-cell kit-table-cell-clamp" style="--kit-lines:1"');
  });

  it("keeps a panel's explanation behind About, and makes its title the link to the list", () => {
    const panel = html(createElement(Panel, { title: "Recent", to: "/tasks", about: "What this panel is for.", children: "body" }));
    expect(panel).toContain("<span>About</span>");
    expect(panel).not.toContain("What this panel is for.");
    expect(panel).toMatch(/<h2><a href="\/tasks"[^>]*>Recent<\/a><\/h2>/);
    expect(panel).not.toContain("View all");
  });
});

describe("related links", () => {
  it("are one line of links, and nothing at all when there are none", () => {
    const line = html(createElement(RelatedLinks, { label: "Views", links: [{ label: "What runs when", to: "/views/schedules-board" }] }));
    expect(line).toContain('<nav class="kit-related" aria-label="Views"><span>Views</span>');
    expect(line).toMatch(/<a href="\/views\/schedules-board"[^>]*>What runs when<\/a>/);
    expect(html(createElement(RelatedLinks, { label: "Views", links: [] }))).toBe("");
  });
});
