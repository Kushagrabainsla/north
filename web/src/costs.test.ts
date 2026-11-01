import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { InferenceCategories, unpricedNote } from "./components";
import type { InferenceCategory } from "./types";

// A call to a model North has no price for records no cost. Shown as $0 it
// would read as free; the cost says how many calls it leaves out.
describe("unpriced calls", () => {
  it("are named after a cost that leaves them out", () => {
    expect(unpricedNote(2)).toBe(" + 2 unpriced");
    expect(unpricedNote(0)).toBe("");
    expect(unpricedNote(undefined)).toBe("");
  });

  it("are named in a task's model calls", () => {
    const category: InferenceCategory = {
      category: "agent", calls: 3, tokens_in: 900, tokens_out: 90, cached_tokens: 0,
      cost_usd: 0.0012, unpriced_calls: 1, components: ["general"], models: ["claude-new-model"],
    };
    const html = renderToStaticMarkup(createElement(InferenceCategories, { categories: [category] }));
    expect(html).toContain("$0.0012 · 1 unpriced");
  });
});
