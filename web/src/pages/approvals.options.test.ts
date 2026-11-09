import { describe, expect, it } from "vitest";
import { hasOwnOptions } from "./Approvals";

describe("hasOwnOptions", () => {
  it("is false for a plain yes or no", () => {
    expect(hasOwnOptions(["Approve", "Reject"])).toBe(false);
    expect(hasOwnOptions([])).toBe(false);
  });

  it("is true for a card that acts on the option you choose", () => {
    expect(hasOwnOptions(["Resume", "Discard"])).toBe(true);
  });
});
