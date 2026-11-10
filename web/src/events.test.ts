// One row for a ledger event on Tasks, in a chat turn and on the dashboard. The dashboard's
// activity strip was a 4-column grid that gave one event a quarter of the row, cut short (#76, P16);
// it is these rows now, each the full width.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { EventRow } from "./events";
import type { LedgerEntry } from "./types";

const entry = { id: "e1", action: "tool_called", source: "agent", agent: "north", status: "failed", task_id: "t1",
  timestamp: new Date().toISOString(), output: "not found", input: "", model_used: "glm", duration_ms: 1500 } as unknown as LedgerEntry;
const html = (props: Record<string, unknown>) =>
  renderToStaticMarkup(createElement(MemoryRouter, null, createElement(EventRow, { entry, ...props })));

describe("an event row", () => {
  it("says what happened, how it ended in its colour, and what it returned", () => {
    const row = html({});
    expect(row).toContain("tool called");
    expect(row).toContain('kit-status kit-tone-bad">failed</span>');
    expect(row).toContain("not found");
    expect(row).toContain("1.5s");
    expect(row).not.toContain("/tasks/t1");
  });

  it("links its task in the unscoped stream", () => {
    expect(html({ showTask: true })).toContain('href="/tasks/t1"');
  });

  it("is one short line on the dashboard", () => {
    const row = html({ brief: true });
    expect(row).toContain("now");
    expect(row).not.toContain("not found");
    expect(row).not.toContain("glm");
  });
});
