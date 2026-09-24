// The one piece of real logic in the sessions list: which empty-state copy to
// show. It depends on *why* the list is empty, and getting it wrong either
// tells a lie ("no archived sessions" on the active tab with none created
// yet) or hides a search that just has no results.

import { describe, expect, it } from "vitest";
import { sessionsEmptyMessage } from "./Chat";

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
