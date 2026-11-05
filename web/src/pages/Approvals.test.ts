// A card that asks several questions is sent once, with every answer: Send answers
// stays off until each question has one.

import { describe, expect, it } from "vitest";
import { answersComplete } from "./Approvals";
import type { Approval, CardField } from "../types";

const question = (name: string): CardField =>
  ({ name, label: `${name}?`, type: "text", value: "", editable: true, options: [] }) as CardField;

const card = { type: "question", fields: [question("q1"), question("q2")] } as unknown as Approval;

describe("sending several answers", () => {
  it("waits until every question is answered", () => {
    expect(answersComplete(card, { q1: "Manual only", q2: "" })).toBe(false);
    expect(answersComplete(card, { q1: "Manual only", q2: "   " })).toBe(false);
    expect(answersComplete(card, { q1: "Manual only", q2: "~/Reading" })).toBe(true);
  });
});
