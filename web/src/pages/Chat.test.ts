// The one piece of real logic in the sessions list: which empty-state copy to
// show. It depends on *why* the list is empty, and getting it wrong either
// tells a lie ("no archived sessions" on the active tab with none created
// yet) or hides a search that just has no results.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { attachmentText, dictationProblem, ghostText, joinedTurn, replyText, Sessions, sessionsEmptyMessage, turnStatus } from "./Chat";
import { optionParts } from "../kit";
import { DialogProvider } from "../dialog";
import type { Turn } from "../types";

// What each endpoint returns in the render tests; a path left out is still loading.
const served = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: served.data[path] ?? null, loading: !(path in served.data), error: "", reload: async () => {} }),
}));

const sessionsPage = () => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(DialogProvider, null, createElement(Sessions))));

describe("the sessions list", () => {
  const session = { id: "c1", title: "Trip planning", goal: "", goal_status: "active", source: "web", workspace: "", pinned: true,
    archived: false, turn_count: 3, updated_at: "" };

  it("keeps its header while the list loads", () => {
    served.data = {};
    const html = sessionsPage();
    expect(html).toContain("<h1>Sessions</h1>");
    expect(html).toContain('class="kit-button kit-button-primary kit-button-md"><span>+ New session</span>');
    expect(html).toContain("kit-loading");
  });

  it("opens a session from its row, with pin, archive and a red delete beside it", () => {
    served.data = { "/web/api/conversations?archived=false": [session] };
    const html = sessionsPage();
    expect(html).toMatch(/<a class="kit-table-open" href="\/sessions\/c1"[^>]*>.*Trip planning/);
    expect(html).toContain('aria-label="Unpin session"');
    expect(html).toContain('aria-label="Archive session"');
    expect(html).toMatch(/class="kit-button kit-button-danger kit-button-sm"[^>]*aria-label="Delete session"/);
    expect(html).toContain('aria-label="Search sessions"');
    expect(html).toContain("1 session<");
  });

  it("says why the list is empty", () => {
    served.data = { "/web/api/conversations?archived=false": [] };
    expect(sessionsPage()).toContain("No sessions have been created yet.");
  });
});

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

describe("a paused task that still has a card", () => {
  it("reads paused, card or not", () => {
    // It read "waiting for approval", so the pause was invisible until the API was asked.
    expect(turnStatus("paused", true)).toBe("paused");
    expect(ghostText(true, "half a thought", [])).toBe("Paused.");
  });

  it("lets a card win over a running task, and keeps a finished task's outcome", () => {
    expect(turnStatus("running", true)).toBe("waiting_for_approval");
    expect(turnStatus("completed", false, "waiting_for_user")).toBe("waiting_for_user");
    expect(turnStatus("completed", false)).toBe("response_ready");
  });
});

describe("the grey line while North works", () => {
  const searching = [{ icon: "⚙", text: "Web search: lab roles", active: true }];

  it("shows North's newest thought, in full", () => {
    expect(ghostText(false, "They want help.\n\nTheir goals point to a lab role.", searching)).toBe("Their goals point to a lab role.");
  });

  it("shows what North is doing before it has thought out loud", () => {
    expect(ghostText(false, "", searching)).toBe("Web search: lab roles…");
    expect(ghostText(false, "", [])).toBe("Starting…");
  });
});

describe("an offered answer", () => {
  it("reads as a title with its explanation under it", () => {
    expect(optionParts("North-native: flows that find jobs, on a schedule")).toEqual(["North-native", "flows that find jobs, on a schedule"]);
    expect(optionParts("Prep help only")).toEqual(["Prep help only", ""]);
  });
});

describe("dictation that cannot start", () => {
  it("says why, instead of leaving the button as it was", () => {
    // Issue #76, W12: with no usable microphone or speech service, nothing at all happened.
    expect(dictationProblem("audio-capture")).toBe("No microphone was found.");
    expect(dictationProblem("not-allowed")).toContain("not allowed to use the microphone");
    expect(dictationProblem("bad-grammar")).toBe("Dictation stopped: bad-grammar.");
  });
});
