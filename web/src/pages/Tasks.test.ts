// The one piece of real logic on the merged page: putting the ledger back into
// the runs it came from. The ledger is a flat stream ordered newest-first, and
// every field the task list shows has to be recovered from it.

import { describe, expect, it } from "vitest";
import { isTerminal, summarise } from "./Tasks";
import type { LedgerEntry } from "../types";

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

describe("what counts as the end of a task", () => {
  it("recognises every terminal action", () => {
    for (const action of ["task_completed", "task_completed_with_failures", "task_failed", "task_cancelled"]) {
      expect(isTerminal(entry({ action }))).toBe(true);
    }
  });

  it("does not mistake an agent finishing for the task finishing", () => {
    // One agent of several completing is not the run ending, and treating it as
    // such would report a task done while it was still working.
    expect(isTerminal(entry({ action: "agent_completed" }))).toBe(false);
    expect(isTerminal(entry({ action: "task_received" }))).toBe(false);
    expect(isTerminal(entry({}))).toBe(false);
  });
});
