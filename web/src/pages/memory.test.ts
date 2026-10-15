import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { formatDay } from "../components";
import { factDates, LearningStatus } from "./Memory";

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

describe("fact dates", () => {
  const base = { id: "f", content: "x", category: "jobs", confidence: 0.8, status: "active", updated_at: "", valid_from: null, valid_until: null, ended: false };

  it("never shifts a calendar day by the time zone", () => {
    expect(formatDay("2026-08-14")).toContain("14");
  });

  it("says when a fact is true, and when it has ended", () => {
    expect(factDates({ ...base, valid_until: "2026-08-14", ended: true })).toMatch(/^ended .*14/);
    expect(factDates({ ...base, valid_from: "2026-05-26", valid_until: "2099-08-14" })).toMatch(/^true .*26.* – .*14/);
    expect(factDates({ ...base, valid_until: "2099-08-14" })).toMatch(/^true until/);
    expect(factDates({ ...base, valid_from: "2026-05-26" })).toMatch(/^true from/);
    expect(factDates(base)).toBe("");
    expect(factDates({ ...base, status: "superseded" })).toBe("replaced by a newer fact");
  });
});
