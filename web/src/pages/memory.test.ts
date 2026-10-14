import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { LearningStatus } from "./Memory";

let learning: unknown;

vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: () => ({ data: learning, loading: false, error: "", reload: async () => {} }),
}));

describe("learning status", () => {
  it("says how many of your messages north has not read yet", () => {
    learning = { read_up_to: "2026-09-29T00:55:35+00:00", waiting: 12, more: false };
    const markup = renderToStaticMarkup(createElement(LearningStatus));
    expect(markup).toContain("12 messages not read yet");
    expect(markup).toContain("read up to");
  });

  it("says when it has caught up", () => {
    learning = { read_up_to: "2026-10-14T19:00:00+00:00", waiting: 0, more: false };
    expect(renderToStaticMarkup(createElement(LearningStatus))).toContain("up to date");
  });

  it("marks a count that stopped at the cap", () => {
    learning = { read_up_to: null, waiting: 500, more: true };
    expect(renderToStaticMarkup(createElement(LearningStatus))).toContain("500+ messages not read yet");
  });
});
