// The one piece of real logic in the sessions list: which empty-state copy to
// show. It depends on *why* the list is empty, and getting it wrong either
// tells a lie ("no archived sessions" on the active tab with none created
// yet) or hides a search that just has no results.

import { describe, expect, it } from "vitest";
import { replyText, sessionsEmptyMessage } from "./Chat";

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
