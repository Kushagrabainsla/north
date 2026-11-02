import { describe, expect, it } from "vitest";
import { dialsLine, memoryLine } from "./Dashboard";

// Issue #76, P12 and P14: the System tile said "Power cruise · ask" while a pinned model decided and
// Settings said power was not in use; the Memory tile said "4 context documents" over an empty memory.
describe("the dashboard's tiles say what is so", () => {
  it("names the pinned model, not power, under manual routing", () => {
    const pinned = { status: "online", power: "cruise", autonomy: "ask", routing: "manual", model: "anthropic:claude-haiku-5-5" };
    expect(dialsLine(pinned)).toBe("Model claude-haiku-5-5 (pinned) · ask");
    expect(dialsLine({ ...pinned, routing: "auto", model: null })).toBe("Power cruise · ask");
  });

  it("counts what memory holds", () => {
    expect(memoryLine({ facts: 0, documents: 0, of_documents: 2 })).toEqual({
      count: 0,
      label: "facts about you",
      note: "No north stars or judgement rules written yet.",
    });
    expect(memoryLine({ facts: 1, documents: 1, of_documents: 2 }).note).toBe(
      "1 of 2 documents written: north stars and judgement rules.",
    );
  });
});
