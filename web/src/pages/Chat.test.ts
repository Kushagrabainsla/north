// The one piece of real logic in the sessions list: which empty-state copy to
// show. It depends on *why* the list is empty, and getting it wrong either
// tells a lie ("no archived sessions" on the active tab with none created
// yet) or hides a search that just has no results.

import { describe, expect, it } from "vitest";
import { attachmentText, joinedTurn, replyText, sessionsEmptyMessage } from "./Chat";
import type { Turn } from "../types";

describe("what can be attached to a message", () => {
  const bytes = (values: number[]) => new Uint8Array(values).buffer;

  it("attaches text, including non-ASCII text", () => {
    expect(attachmentText(new TextEncoder().encode("notes: café ✓").buffer)).toBe("notes: café ✓");
  });

  it("refuses an image instead of pasting its bytes as text", () => {
    const pngHeader = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d];
    expect(attachmentText(bytes(pngHeader))).toBeNull();
  });

  it("refuses a binary file that happens to be valid UTF-8", () => {
    expect(attachmentText(bytes([0x41, 0x00, 0x42]))).toBeNull();
  });
});

describe("sessions empty-state copy", () => {
  it("blames the search first, regardless of which tab is open", () => {
    expect(sessionsEmptyMessage("standup notes", "active")).toBe("No sessions match this search.");
    expect(sessionsEmptyMessage("standup notes", "archived")).toBe("No sessions match this search.");
  });

  it("distinguishes a genuinely empty archive from a fresh install", () => {
    expect(sessionsEmptyMessage("", "archived")).toBe("No archived sessions.");
    expect(sessionsEmptyMessage("", "active")).toBe("No sessions have been created yet.");
  });
});

describe("which reply a turn shows", () => {
  it("shows live text while the task runs", () => {
    expect(replyText(true, "Opening LinkedIn…", "")).toBe("Opening LinkedIn…");
  });

  it("shows the saved answer once the task ends, not the stream pieced together live", () => {
    const streamed = "> **Note:** 1 agent(s) did not complete.LinkedIn is open";
    expect(replyText(false, streamed, "LinkedIn is open")).toBe("LinkedIn is open");
  });

  it("falls back to whatever exists", () => {
    expect(replyText(false, "only streamed", "")).toBe("only streamed");
    expect(replyText(true, "", "only saved")).toBe("only saved");
  });
});

describe("a message sent into a running task", () => {
  const turn = (id: string, task_id: string | null) => ({ id, task_id, prompt: id, created_at: "" }) as unknown as Turn;
  const turns = [turn("first", "task-1"), turn("added", "task-1"), turn("next", "task-2")];

  it("shows as part of the task an earlier turn started, with no answer of its own", () => {
    expect(joinedTurn(turns, 1)).toBe(true);
  });

  it("leaves a turn that started its own task as it was", () => {
    expect(joinedTurn(turns, 0)).toBe(false);
    expect(joinedTurn(turns, 2)).toBe(false);
  });
});
