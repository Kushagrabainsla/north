import { describe, expect, it } from "vitest";
import { oldestFirst, runNeedsYou, waitedLong, waitingFor } from "./inbox";
import { score } from "./palette";
import { firstLine } from "./pages/Approvals";
import { memoryTabFor, MEMORY_TABS } from "./pages/Memory";
import { taskTitle } from "./pages/Tasks";

const ago = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();

describe("the Inbox", () => {
  it("counts a paused or waiting flow run as needing you, and no other run", () => {
    expect(["paused", "waiting", "completed", "cancelled", "running"].filter(status => runNeedsYou({ status })))
      .toEqual(["paused", "waiting"]);
  });

  it("shows the item that waited longest first", () => {
    const items = [{ id: "new", at: ago(1) }, { id: "old", at: ago(50) }, { id: "mid", at: ago(5) }];
    expect(oldestFirst(items, item => item.at).map(item => item.id)).toEqual(["old", "mid", "new"]);
  });

  it("says how long something waited, in hours and then days", () => {
    expect(waitingFor(ago(0.2))).toBe("waiting under 1 h");
    expect(waitingFor(ago(30))).toBe("waiting 30 h");
    expect(waitingFor(ago(72))).toBe("waiting 3 days");
  });

  it("calls more than a day a long wait", () => {
    expect(waitedLong(ago(23))).toBe(false);
    expect(waitedLong(ago(25))).toBe(true);
  });

  it("shows one line of a long text, and the rest on demand", () => {
    expect(firstLine("\n## Step 'check' failed\nsecond line")).toBe("Step 'check' failed");
    expect(firstLine("x".repeat(200))).toHaveLength(140);
  });
});

describe("search and jump", () => {
  it("ranks a match at the start of a word above one inside it, and drops a miss", () => {
    expect(score("nightly-check", "check")).toBeGreaterThan(score("nightcheckly", "check"));
    expect(score("Settings · Models", "set mod")).toBeGreaterThan(0);
    expect(score("Flows", "memory")).toBe(-1);
  });
});

describe("the Memory page has one row of tabs", () => {
  it("has each document, the facts, the episodes and what north learned from your decisions", () => {
    expect(MEMORY_TABS.map(([, label]) => label)).toEqual(
      ["North stars", "Judgement rules", "Soul", "Facts", "Episodes", "Learned decisions", "Safe actions"]);
  });

  it("opens the tab an old or outside link means", () => {
    expect(memoryTabFor("documents", "soul.md")).toBe("soul.md");
    expect(memoryTabFor("documents", null)).toBe("north_stars.md");
    expect(memoryTabFor("approvals", null)).toBe("decisions");
    expect(memoryTabFor("rules", null)).toBe("rules");
    expect(memoryTabFor("nonsense", null)).toBeNull();
  });
});

describe("a task's name", () => {
  it("uses the prompt, and names north's own work instead of showing its id twice", () => {
    expect(taskTitle({ prompt: "built it", id: "task_1" })).toBe("built it");
    expect(taskTitle({ prompt: "flow_5b7a", id: "flow_5b7a" })).toBe("Flow run");
    expect(taskTitle({ prompt: "", id: "521993c4" })).toBe("Background task");
  });
});
