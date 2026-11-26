// A view north made is drawn from the kit: each A2UI part by the kit part of its role, its data
// read through bindings, a template repeated per item, and nothing drawn that the kit lacks.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { resolvePath, steadyOrder, ViewSurface, type ViewComponent, type ViewMessage } from "./views";
import { viewsAbout } from "./pages/Views";

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

describe("a list that comes back in a new order", () => {
  // The Kasa panel: the tool lists lights in the order the network answers, and the panel reads it every
  // 30 s, so the row under the pointer became another light between look and click.
  const lights = (...ids: string[]) => ids.map((id) => ({ id, name: `lamp ${id}` })) as never[];
  const drawn = (items: never[], order: number[]) => order.map((index) => (items[index] as { id: string }).id);

  it("keeps each item in the place it had when first shown", () => {
    const first = steadyOrder(lights("a", "b", "c", "d"));
    expect(first.order).toEqual([0, 1, 2, 3]);
    const again = lights("c", "a", "d", "b");
    expect(drawn(again, steadyOrder(again, first.known).order)).toEqual(["a", "b", "c", "d"]);
  });

  it("puts a new item after the known ones, and gives one that was missing its old place back", () => {
    const first = steadyOrder(lights("a", "b", "c"));
    const missing = lights("c", "a");
    const second = steadyOrder(missing, first.known);
    expect(drawn(missing, second.order)).toEqual(["a", "c"]);
    const back = lights("e", "b", "c", "a");
    expect(drawn(back, steadyOrder(back, second.known).order)).toEqual(["a", "b", "c", "e"]);
  });

  it("is drawn as it comes when nothing says which item is which", () => {
    const plain = [{ name: "x" }, { name: "y" }] as never[];
    expect(steadyOrder(plain, ["y", "x"]).order).toEqual([0, 1]);
    const twice = lights("a", "a");
    expect(steadyOrder(twice, ["a"]).order).toEqual([0, 1]);
  });

  it("still reads each row from its own item", () => {
    const page = html(surface([
      { id: "root", component: "Panel", title: "Lights", children: { path: "/devices", componentId: "row" } },
      { id: "row", component: "ListItem", title: { path: "name" } },
    ], { devices: lights("b", "a") }));
    expect(page.indexOf("lamp b")).toBeLessThan(page.indexOf("lamp a"));
  });
});

describe("the views about a flow, skill or tool", () => {
  it("are the ones tied to that kind and name", () => {
    const view = (id: string, about: { kind: "flow" | "skill" | "tool"; name: string } | null) =>
      ({ id, title: id, description: "", about, updated_at: "", reads: [], actions: 0 });
    const views = [view("a", { kind: "flow", name: "nightly-cleanup" }), view("b", { kind: "skill", name: "nightly-cleanup" }), view("c", null)];
    expect(viewsAbout(views, "flow", "nightly-cleanup").map(v => v.id)).toEqual(["a"]);
    expect(viewsAbout(views, "tool", "nightly-cleanup")).toEqual([]);
  });
});
