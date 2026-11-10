// A card that asks several questions is sent once, with every answer: Send answers
// stays off until each question has one.

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../dialog";
import { ApprovalMemoryPanel, SafeActionsPanel } from "./Approvals";
import { answersComplete } from "./approvalCard";
import type { Approval, CardField } from "../types";

const question = (name: string): CardField =>
  ({ name, label: `${name}?`, type: "text", value: "", editable: true, options: [] }) as CardField;

const card = { type: "question", fields: [question("q1"), question("q2")] } as unknown as Approval;

describe("sending several answers", () => {
  it("waits until every question is answered", () => {
    expect(answersComplete(card, { q1: "Manual only", q2: "" })).toBe(false);
    expect(answersComplete(card, { q1: "Manual only", q2: "   " })).toBe(false);
    expect(answersComplete(card, { q1: "Manual only", q2: "~/Reading" })).toBe(true);
  });
});

const served: Record<string, unknown> = {};

vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: served[path] ?? null, loading: false, error: "", reload: async () => {} }),
}));

const render = (panel: ReactElement) => renderToStaticMarkup(createElement(DialogProvider, null, panel));

// Every removal is a red button that asks first (confirmDelete); a rule that can be
// added back says "Remove", one of your own says "Delete".
describe("removing what north learned or may do", () => {
  it("forgets a learned decision with a red button, its decision as a status word", () => {
    served["/web/api/memory/approvals"] = [
      { fingerprint: "f1", agent: "git", signature: "git status", decision: "rejected", count: 2, updated_at: "" },
    ];
    const html = render(createElement(ApprovalMemoryPanel));
    expect(html).toContain('class="kit-row"');
    expect(html).toContain('kit-status kit-tone-bad">rejected</span>');
    expect(html).toContain("2×");
    expect(html).toContain('kit-button-danger kit-button-sm"><span>Forget</span>');
  });

  it("removes a shipped rule and deletes your own, both in red", () => {
    served["/web/api/unattended/rules"] = {
      mode: "ask", active: false, kinds: ["command"],
      rules: [
        { id: "r1", kind: "command", pattern: "make lint", enabled: true, source: "builtin", note: "", fire_count: 0, last_fired_at: null },
        { id: "r2", kind: "command", pattern: "npm test", enabled: false, source: "user", note: "", fire_count: 3, last_fired_at: null },
      ],
    };
    const html = render(createElement(SafeActionsPanel));
    expect(html).toContain('kit-button-danger kit-button-sm"><span>Remove</span>');
    expect(html).toContain('kit-button-danger kit-button-sm"><span>Delete</span>');
    // A switched-off rule reads quieter, and says so.
    expect(html).toContain("kit-row kit-row-muted");
    expect(html).toContain("<span>disabled</span>");
    // Rules that cannot fire in this mode say why.
    expect(html).toContain("kit-notice-warning");
    expect(html).toContain("<span>Add rule</span>");
  });
});
