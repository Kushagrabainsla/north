import { describe, expect, it } from "vitest";
import { lastUpdateText } from "./System";

describe("how the last update went", () => {
  it("says so when north was never updated from here", () => {
    expect(lastUpdateText(null)).toBe("never updated from here");
  });

  it("names the outcome and when", () => {
    const at = "Nov 9, 8:17 PM";
    expect(lastUpdateText({ state: "updated", detail: "", at })).toBe("updated Nov 9, 8:17 PM");
    expect(lastUpdateText({ state: "failed", detail: "Install failed", at })).toBe("failed Nov 9, 8:17 PM");
    expect(lastUpdateText({ state: "running", detail: "", at })).toBe("updating since Nov 9, 8:17 PM");
    expect(lastUpdateText({ state: "cancelled", detail: "", at })).toBe("cancelled Nov 9, 8:17 PM");
  });
});
