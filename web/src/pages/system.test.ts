import { describe, expect, it } from "vitest";
import { endpointLines, rollUp } from "./routing";
import { chainModelFacts, lastUpdateText } from "./System";

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

describe("a model chain rung", () => {
  it("names who serves it, its price and its score", () => {
    const model = { model: "m", score: 0.91234, price: 0.000002, providers: ["openai", "azure"], available: true, skipped_because: "" };
    expect(chainModelFacts(model)).toEqual(["openai, azure", "$0.000002/tok", "score 0.912"]);
    expect(chainModelFacts({ ...model, price: null })[1]).toBe("free");
  });
});

describe("a routing walk as table lines", () => {
  it("lists each provider's calls before what it skipped, with counts", () => {
    const lines = endpointLines(rollUp([
      { model: "a", provider: "groq", reason: "rate limited", tried: true, status_code: 429, detail: "slow down" },
      { model: "b", provider: "groq", reason: "rate limited", tried: true, status_code: 429 },
      { model: "c", provider: "groq", reason: "no tools" },
      { model: "d", provider: "openai", reason: "too small" },
    ]));
    expect(lines.map(({ provider, outcome, reason, detail }) => [provider, outcome, reason, detail])).toEqual([
      ["groq", "2 called", "429 · rate limited", "slow down"],
      ["groq", "1 skipped", "no tools", ""],
      ["openai", "1 skipped", "too small", ""],
    ]);
    expect(new Set(lines.map(line => line.key)).size).toBe(lines.length);
  });
});
