// The one piece of real logic on the merged page: putting the ledger back into
// the runs it came from. The ledger is a flat stream ordered newest-first, and
// every field the task list shows has to be recovered from it.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { turnStatus } from "./Chat";
import { runFacts, summarise, taskStatus, Tasks } from "./Tasks";
import { ArtifactGrid } from "./Artifacts";
import type { AgentRun, Artifact, LedgerEntry } from "../types";

// What each endpoint returns in the render tests; a path left out is still loading.
const served = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: served.data[path] ?? null, loading: !(path in served.data), error: "", reload: async () => {} }),
}));

const page = (url: string) => renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [url] },
  createElement(Routes, null,
    createElement(Route, { path: "/tasks", element: createElement(Tasks) }),
    createElement(Route, { path: "/tasks/:taskId", element: createElement(Tasks) }))));

let counter = 0;
const entry = (over: Partial<LedgerEntry>): LedgerEntry => ({
  id: `e${counter++}`,
  timestamp: "2026-09-09T00:00:00Z",
  source: "agent",
  ...over,
});

describe("grouping the ledger back into tasks", () => {
  it("finds the prompt at the far end of a newest-first stream", () => {
    // The naive read - "the first task_received" - works only because the
    // stream happens to be reversed, and silently picks a retry's prompt when
    // it is not.
    const [task] = summarise([
      entry({ task_id: "t1", action: "task_completed", status: "completed" }),
      entry({ task_id: "t1", action: "agent_completed" }),
      entry({ task_id: "t1", action: "task_received", input: "Book the flight" }),
    ]);

    expect(task.prompt).toBe("Book the flight");
  });

  it("lists as agents only the agents that worked on the task", () => {
    const [task] = summarise([
      entry({ task_id: "t1", source: "agent", agent: "general", action: "agent_completed" }),
      entry({ task_id: "t1", source: "inference_router", agent: "agent", action: "inference_call" }),
      entry({ task_id: "t1", source: "approval", agent: "kasa", action: "approval_responded: approved" }),
      entry({ task_id: "t1", source: "prompt", agent: "user", action: "user_message" }),
      entry({ task_id: "t1", source: "inference_router", agent: "planning", action: "inference_call" }),
    ]);

    expect(task.agents).toEqual(["general"]);
  });

  it("falls back to the id when no prompt was recorded", () => {
    expect(summarise([entry({ task_id: "t1", action: "agent_started" })])[0].prompt).toBe("t1");
  });

  it("takes the status from whichever entry ended the task", () => {
    const [task] = summarise([
      entry({ task_id: "t1", action: "task_failed", status: "failed" }),
      entry({ task_id: "t1", action: "agent_completed", status: "completed" }),
    ]);

    expect(task.status).toBe("failed");
  });

  it("reads a task with no ending as still running", () => {
    expect(summarise([entry({ task_id: "t1", action: "agent_started" })])[0].status).toBe("running");
  });

  it("shows queued and retrying lifecycle states instead of generic running", () => {
    expect(summarise([entry({ task_id: "t1", action: "task_queued", status: "pending" })])[0].status).toBe("queued");
    expect(summarise([entry({ task_id: "t1", action: "task_retrying", status: "pending" })])[0].status).toBe("retrying");
  });

  it("shows exhausted recovery as needs attention", () => {
    expect(summarise([entry({ task_id: "t1", action: "task_needs_attention", status: "failed" })])[0].status)
      .toBe("needs_attention");
  });

  it("keeps each task's events with that task", () => {
    const tasks = summarise([
      entry({ task_id: "t1", action: "task_received", input: "one" }),
      entry({ task_id: "t2", action: "task_received", input: "two" }),
      entry({ task_id: "t1", action: "agent_started" }),
    ]);

    expect(tasks.map(t => t.id)).toEqual(["t1", "t2"]);
    expect(tasks[0].entries).toHaveLength(2);
  });

  it("lists each agent once, in the order it first appears", () => {
    const [task] = summarise([
      entry({ task_id: "t1", agent: "coder" }),
      entry({ task_id: "t1", agent: "reviewer" }),
      entry({ task_id: "t1", agent: "coder" }),
    ]);

    expect(task.agents).toEqual(["coder", "reviewer"]);
  });

  it("leaves out events that belong to no task", () => {
    // north's own events - startup, cron ticks, recovery sweeps. They are not
    // runs, which is why the page keeps a second view for them.
    expect(summarise([entry({ action: "startup_recovery_sweep" }), entry({ task_id: "", action: "boot" })])).toEqual([]);
  });
});

describe("a task whose work was blocked", () => {
  it("is listed as blocked, not as completed work, as the chat shows it", () => {
    const rows = [
      { id: "e2", timestamp: "2026-11-02T10:00:01Z", source: "system", task_id: "t1", action: "task_completed", error_type: "blocked" },
      { id: "e1", timestamp: "2026-11-02T10:00:00Z", source: "prompt", task_id: "t1", action: "task_received", input: "save my plan" },
    ] as LedgerEntry[];

    expect(taskStatus(rows)).toBe("blocked");
    expect(turnStatus("completed", false, "blocked")).toBe("blocked");
  });
});

describe("the task list", () => {
  it("keeps its header while the ledger loads", () => {
    served.data = {};
    const html = page("/tasks");
    expect(html).toContain("<h1>Tasks</h1>");
    expect(html).toContain("+ New task");
    expect(html).toContain("kit-loading");
  });

  it("lists each task as a row that opens it, under the kit's tabs and search", () => {
    served.data = { "/orchestrator/ledger?limit=500": [
      { id: "e1", timestamp: "2026-11-02T10:00:00Z", source: "prompt", task_id: "t1", action: "task_received", input: "Book the flight" },
    ] };
    const html = page("/tasks");
    expect(html).toContain('role="tablist" aria-label="Task views"');
    expect(html).toContain('aria-selected="true" class="kit-tab kit-tab-active">All work');
    expect(html).toContain('aria-label="Filter tasks"');
    expect(html).toMatch(/<a class="kit-table-open" href="\/tasks\/t1"[^>]*>.*Book the flight/);
  });

  it("says why the list is empty", () => {
    served.data = { "/orchestrator/ledger?limit=500": [] };
    expect(page("/tasks")).toContain("No task history yet.");
  });
});

describe("one task's page", () => {
  it("keeps its header and the way back while the task loads", () => {
    served.data = {};
    const html = page("/tasks/t1");
    expect(html).toContain("<h1>t1</h1>");
    expect(html).toMatch(/<a [^>]*class="kit-button kit-button-secondary kit-button-md[^"]*" href="\/tasks"[^>]*><span>← All tasks<\/span><\/a>/);
    expect(html).toContain("kit-loading");
  });
});

describe("an agent run's facts", () => {
  const run = (over: Partial<AgentRun>) => ({
    run_id: "r1", agent: "general", status: "completed", attempt: 1, models_used: [], tokens_in: 10, tokens_out: 5,
    cost_usd: 0, skills: [], ...over,
  }) as AgentRun;

  it("joins what is known and leaves out what is not", () => {
    expect(runFacts(run({}))).toBe("no model recorded · 15 tokens");
    expect(runFacts(run({ models_used: ["gpt-5"], providers_used: ["codex"], duration_ms: 1500, cost_usd: 0.01 })))
      .toBe("gpt-5 · codex · 1.5s · 15 tokens · $0.0100");
  });
});

describe("what a task produced", () => {
  it("shows each file as the same card the Artifacts page opens", () => {
    const file = { id: "a1", name: "plan.md", kind: "notes", task: "t1", media_type: "text/markdown", updated_at: 0 } as Artifact;
    const html = renderToStaticMarkup(createElement(ArtifactGrid, { files: [file] }));
    expect(html).toContain('class="kit-card-grid"');
    expect(html).toMatch(/<button type="button" class="kit-card-head"><span class="kit-card-icon">✎<\/span><b>plan.md<\/b><small>notes<\/small><\/button>/);
  });
});
