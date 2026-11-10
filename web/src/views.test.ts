// A view north made is drawn from the kit: each A2UI part by the kit part of its role, its data
// read through bindings, a template repeated per item, and nothing drawn that the kit lacks.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { resolvePath, ViewSurface, type ViewComponent, type ViewMessage } from "./views";

const surface = (components: ViewComponent[], data: unknown = {}): ViewMessage[] => [
  { version: "v0.9", createSurface: { surfaceId: "s", catalogId: "north:kit/v1" } },
  { version: "v0.9", updateComponents: { surfaceId: "s", components } },
  { version: "v0.9", updateDataModel: { surfaceId: "s", path: "/", value: data as never } },
];
const html = (messages: ViewMessage[], onAction?: (name: string) => void) =>
  renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ViewSurface, { messages, onAction })));

const SCHEDULES = {
  schedules: { recurring: [
    { name: "nightly-cleanup", task: "Housekeeping", schedule: "daily at 03:00", next_run: "in 17 h", state: "active" },
    { name: "waiting-cards", task: "Reminder", schedule: "daily at 09:00", next_run: "in 23 h", state: "paused" },
  ] },
};

describe("a view", () => {
  it("repeats a template for each item, reading each item's own fields", () => {
    const page = html(surface([
      { id: "root", component: "Panel", title: "Schedules", children: { path: "/schedules/recurring", componentId: "row" } },
      { id: "row", component: "ListItem", title: { path: "name" }, description: { path: "task" }, meta: [{ path: "next_run" }], status: { path: "state" } },
    ], SCHEDULES));
    expect(page).toContain("<h2>Schedules</h2>");
    expect(page.match(/class="kit-row"/g)?.length).toBe(2);
    expect(page).toContain("nightly-cleanup");
    expect(page).toContain("in 23 h");
    expect(page).toContain('kit-status kit-tone-good">active</span>');
    expect(page).toContain('kit-status kit-tone-warn">paused</span>');
  });

  it("lists rows in a kit table and figures as stat tiles", () => {
    const page = html(surface([
      { id: "root", component: "Column", children: ["figures", "table"] },
      { id: "figures", component: "StatGrid", children: ["count"] },
      { id: "count", component: "Stat", label: "Tasks", value: { path: "/metrics/total" } },
      { id: "table", component: "Table", rows: { path: "/schedules/recurring" }, columns: [{ key: "name", label: "Flow" }, { key: "schedule", label: "When" }] },
    ], { ...SCHEDULES, metrics: { total: 12 } }));
    expect(page).toContain('class="kit-stat"><span>Tasks</span><strong>12</strong>');
    expect(page).toContain('class="kit-table"');
    expect(page).toContain("daily at 03:00");
  });

  it("only offers its buttons once it is added", () => {
    const components: ViewComponent[] = [{ id: "root", component: "Button", label: "Run it", action: { event: { name: "run" } } }];
    expect(html(surface(components))).toContain("disabled");
    expect(html(surface(components), () => undefined)).not.toContain("disabled");
  });

  it("draws nothing the kit lacks, and stops at a loop", () => {
    expect(html(surface([{ id: "root", component: "Iframe" }]))).toContain("a part north&#x27;s kit does not have: Iframe");
    expect(html(surface([{ id: "root", component: "Column", children: ["root"] }]))).toContain("nested too deep");
    expect(html(surface([{ id: "other", component: "Text", text: "x" }]))).toContain("no root part");
  });

  it("reads a path from the top or from the item, as A2UI says", () => {
    const data = { a: { "b/c": [10, 20] } } as never;
    expect(resolvePath(data, "/a/b~1c/1")).toBe(20);
    expect(resolvePath(data, "1", "/a/b~1c")).toBe(20);
    expect(resolvePath(data, "/missing/x")).toBeUndefined();
  });
});
