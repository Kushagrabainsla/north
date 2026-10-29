// The live step list the chat shows while a task runs: the dashboard showed none of this until the
// task ended, and only then in the collapsed Details drawer, refreshed every 5 seconds.

import { describe, expect, it } from "vitest";
import { STEP_EVENTS, foldStep, type Step } from "./liveSteps";

const run = (events: [string, Record<string, unknown>?][]) =>
  events.reduce<Step[]>((steps, [event, data]) => foldStep(steps, event, data), []);

describe("a task's live steps", () => {
  it("shows the plan, the agent and its model, each tool call and its result, in order", () => {
    const steps = run([
      ["classified", { domain: "general", is_consequential: false }],
      ["routed", { agents: ["general"] }],
      ["agent_started", { agent: "general" }],
      ["model", { model: "gpt-oss-120b" }],
      ["tool_called", { tool: "web_search", params: { query: "sales 2025", workspace: "/long/path/that/is/noise" } }],
      ["tool_result", { tool: "web_search", success: true, formatted: "3 results\nfirst…" }],
      ["agent_completed", { agent: "general", summary: "done" }],
    ]);
    expect(steps.map(s => `${s.icon} ${s.text}`)).toEqual([
      "✓ classified: general (direct)",
      "✓ plan ready: general",
      "◎ general running on gpt-oss-120b…",
      "⚙ web_search (query=sales 2025)…",
      "✓ web_search → 3 results first…",
      "✓ general: done",
    ]);
    expect(steps.filter(s => s.active)).toEqual([]);
  });

  it("keeps only the newest step active, and marks the last one failed when the task fails", () => {
    const working = run([["agent_started", { agent: "general" }], ["tool_called", { tool: "bash", params: { command: "pytest" } }]]);
    expect(working.map(s => s.active)).toEqual([false, true]);
    expect(run([...[["agent_started", { agent: "general" }]] as [string, Record<string, unknown>][], ["task_failed", {}]])[0].icon).toBe("✗");
  });

  it("shows what the task waits on, and a delegated agent as a branch", () => {
    const steps = run([["question_required", {}], ["agent_started", { agent: "general", parent_run_id: "r1" }]]);
    expect(steps[0]).toEqual({ icon: "?", text: "waiting for your answer", active: false });
    expect(steps[1].text).toBe("↳ general agent running…");
  });

  it("ignores events it has no line for, and subscribes to every event it folds", () => {
    expect(foldStep([], "prompt_profile", {})).toEqual([]);
    expect(STEP_EVENTS).toContain("tool_called");
    expect(STEP_EVENTS).toContain("question_required");
  });
});
