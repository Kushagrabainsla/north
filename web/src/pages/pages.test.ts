import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { agentDeletionPrompt } from "./Agents";
import { ApprovalCard, memoryLabel, memoryLink } from "./Approvals";
import type { Approval } from "../types";

describe("personal-agent deletion confirmation", () => {
  it("names every schedule and pending job that will be removed", () => {
    const prompt = agentDeletionPrompt("weather", ["Morning forecast", "Rain check"], 2);

    expect(prompt).toContain("Delete personal agent 'weather'?");
    expect(prompt).toContain("• Morning forecast");
    expect(prompt).toContain("• Rain check");
    expect(prompt).toContain("2 pending jobs will also be cancelled.");
  });

  it("does not imply a cascade when there is no future work", () => {
    expect(agentDeletionPrompt("unused", [], 0)).toBe("Delete personal agent 'unused'?");
  });
});

describe("links from a decision to the memory it used", () => {
  const params = (link: string) => new URLSearchParams(link.split("?")[1]);

  it("opens the facts tab on the fact itself", () => {
    const link = memoryLink({ kind: "fact", label: "Fact", text: "You deploy on weekdays", ref: "" });
    expect(link.startsWith("/memory?")).toBe(true);
    expect(params(link).get("tab")).toBe("facts");
    expect(params(link).get("find")).toBe("You deploy on weekdays");
  });

  it("finds a past decision by its fingerprint, not its wording", () => {
    const link = memoryLink({ kind: "past_decision", label: "Your past decision", text: "you approved 'x' (2x)", ref: "abc123" });
    expect(link.startsWith("/approvals?")).toBe(true);
    expect(params(link).get("tab")).toBe("decisions");
    expect(params(link).get("find")).toBe("abc123");
  });

  it("opens the rules document, and the facts the profile is built from", () => {
    expect(params(memoryLink({ kind: "judgement_rules", label: "Your judgement rules", text: "", ref: "" })).get("doc")).toBe("judgement_rules.md");
    const profile = params(memoryLink({ kind: "profile", label: "Your profile", text: "", ref: "" }));
    expect(profile.get("tab")).toBe("facts");
    expect(profile.get("doc")).toBeNull();
  });

  it("names what kind of memory it was, as the API words it", () => {
    expect(memoryLabel({ kind: "fact", label: "Fact", text: "likes tea", ref: "" })).toBe("Fact: likes tea");
    expect(memoryLabel({ kind: "judgement_rules", label: "Your judgement rules", text: "", ref: "" })).toBe("Your judgement rules");
  });
});

describe("answering a question on the approvals page", () => {
  const question = (options: string[]) =>
    ({ id: "q1", type: "question", task_id: "t1", agent: "general", title: "Question", message: "Which quarter?",
       options, status: "pending", chosen_option: "", created_at: "", fields: [], context: "", response: {},
       blocking: true, source: "" }) as unknown as Approval;
  const markup = (options: string[]) =>
    renderToStaticMarkup(createElement(ApprovalCard, { card: question(options), onDecide: () => undefined }));

  it("takes an answer in your own words when the question offers no options", () => {
    // With option buttons only, such a question had no way to be answered here.
    expect(markup([])).toContain('placeholder="Type your answer"');
  });

  it("keeps the offered options as buttons beside it", () => {
    const html = markup(["Q2", "Q3"]);
    expect(html).toContain(">Q2</button>");
    expect(html).toContain('placeholder="Type your answer"');
  });
});
