// The live step list the chat shows while a task runs: the dashboard showed none of this until the
// task ended, and only then in the collapsed Details drawer, refreshed every 5 seconds.

import { describe, expect, it } from "vitest";
import { STEP_EVENTS, clock, currentActivity, foldStep, latestThought, spoken, toolName, type Step } from "./liveSteps";

const run = (events: [string, Record<string, unknown>?][]) =>
  events.reduce<Step[]>((steps, [event, data]) => foldStep(steps, event, data), []);

describe("a task's live steps", () => {
  it("names the plan, the agent and its model, and each tool call as it ended, in plain words", () => {
    const steps = run([
      ["classified", { domain: "general", is_consequential: false }],
      ["routed", { agents: ["general"] }],
      ["agent_started", { agent: "general" }],
      ["model", { model: "gpt-oss-120b" }],
      ["tool_called", { tool: "web_search", params: { workspace: "/long/path/that/is/noise", query: "sales 2025" } }],
      ["tool_result", { tool: "web_search", success: true, formatted: "3 results\nfirst…" }],
      ["agent_completed", { agent: "general", summary: "done" }],
    ]);
    expect(steps.map(s => `${s.icon} ${s.text}`)).toEqual([
      "✓ Understood: a general request",
      "✓ Planned: general",
      "◎ general agent on gpt-oss-120b",
      "✓ Web search: sales 2025 → 3 results first…",
      "✓ general agent finished",
    ]);
    expect(steps.filter(s => s.active)).toEqual([]);
  });

  it("keeps a call's arguments to one short value, never the raw parameters", () => {
    // It showed `ask_user (questions=[{"options":["Something new…)…` and `name=…, schedule=…` lists.
    const [step] = run([["tool_called", { tool: "create_flow", params: { steps: [{ a: 1 }], name: "job-discovery", schedule: "0 8 * * 1-5" } }]]);
    expect(step.text).toBe("Create flow: job-discovery");
  });

  it("says when a call failed, on that call's line", () => {
    const steps = run([
      ["tool_called", { tool: "read_file", params: { path: "resume.md" } }],
      ["tool_result", { tool: "read_file", success: false, error: "not found" }],
    ]);
    expect(steps).toEqual([{ icon: "✗", text: "Read file: resume.md → failed: not found", active: false, tool: "read_file" }]);
  });

  it("leaves a question out of the steps: its card is on the page", () => {
    expect(run([["tool_called", { tool: "ask_user", params: { questions: [] } }], ["tool_result", { tool: "ask_user" }]])).toEqual([]);
  });

  it("keeps only the newest step active, and marks the last one failed when the task fails", () => {
    const working = run([["agent_started", { agent: "general" }], ["tool_called", { tool: "bash", params: { command: "pytest" } }]]);
    expect(working.map(s => s.active)).toEqual([false, true]);
    expect(run([...[["agent_started", { agent: "general" }]] as [string, Record<string, unknown>][], ["task_failed", {}]])[0].icon).toBe("✗");
  });

  it("shows what the task waits on, and a delegated agent as a branch", () => {
    const steps = run([["question_required", {}], ["agent_started", { agent: "general", parent_run_id: "r1" }]]);
    expect(steps[0]).toEqual({ icon: "?", text: "Waiting for your answer", active: false });
    expect(steps[1].text).toBe("↳ general agent started");
  });

  it("ignores events it has no line for, and subscribes to every event it folds", () => {
    expect(foldStep([], "prompt_profile", {})).toEqual([]);
    // Nothing on the server emits failover; a switched model shows as the next "agent on" line.
    expect(STEP_EVENTS).not.toContain("failover");
    expect(STEP_EVENTS).toContain("tool_called");
    expect(STEP_EVENTS).toContain("question_required");
  });
});

describe("what North is doing and thinking", () => {
  it("is the step in progress, else the last one", () => {
    const steps = run([["classifying"], ["tool_called", { tool: "web_search", params: { query: "roles" } }]]);
    expect(currentActivity(steps)).toBe("Web search: roles");
    expect(currentActivity(foldStep(steps, "tool_result", { tool: "web_search" }))).toBe("Web search: roles");
    expect(currentActivity([])).toBe("");
  });

  it("thinks in paragraphs: the newest one, on one line, its end kept when long", () => {
    expect(latestThought("First idea.\n\nSecond\nidea.")).toBe("Second idea.");
    expect(latestThought("   ")).toBe("");
    const long = latestThought(`${"word ".repeat(150)}the end.`);
    expect(long.startsWith("…")).toBe(true);
    expect(long.endsWith("the end.")).toBe(true);
    expect(long.length).toBeLessThanOrEqual(401);
  });
});

describe("times and names", () => {
  it("reads a tool's name as words", () => {
    expect(toolName("web_search")).toBe("Web search");
    expect(toolName("")).toBe("Tool");
  });

  it("reads a duration as a clock and as words", () => {
    expect(clock(24_000)).toBe("0:24");
    expect(clock(3_725_000)).toBe("1:02:05");
    expect(spoken(24_000)).toBe("24s");
    expect(spoken(192_000)).toBe("3m 12s");
    expect(spoken(3_725_000)).toBe("1h 2m");
  });
});
