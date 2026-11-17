// One row for a ledger event on Tasks and in a chat turn. The dashboard's activity strip,
// which used these rows in a short form, is gone (issue #81): its rows said nothing.

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
});
