// A request_secret card: one password field per secret, Save and Not now, and no answer box,
// so nothing typed on it can leave as a reply.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ApprovalCard } from "./approvalCard";
import type { Approval } from "../types";

const card = {
  id: "c1", type: "question", task_id: "t1", agent: "north", title: "North - Secret needed",
  message: "The kasa_lights tool logs in to your TP-Link account.",
  options: ["Done", "Not now"], status: "pending", created_at: new Date().toISOString(), blocking: true,
  fields: [
    { name: "kasa_email", label: "TP-Link email", type: "secret", value: "", editable: false, options: [] },
    { name: "kasa_password", label: "TP-Link password", type: "secret", value: "", editable: false, options: [] },
  ],
} as unknown as Approval;

describe("a secret card", () => {
  const html = renderToStaticMarkup(createElement(ApprovalCard, { card, onDecide: () => undefined }));

  it("asks for each secret in a password field and names where it is kept", () => {
    expect(html.match(/type="password"/g)).toHaveLength(2);
    expect(html).toContain("TP-Link password");
    expect(html).toContain("Kept in north&#x27;s secrets as kasa_password");
  });

  it("offers Save and Not now, and no box to answer in your own words", () => {
    expect(html).toContain("Save");
    expect(html).toContain("Not now");
    expect(html).not.toContain("Your answer");
    expect(html).not.toContain("Answer");
  });
});
