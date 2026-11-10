import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { agentDeletionPrompt } from "./Agents";
import { ApprovalCard, DecisionRow, memoryLabel, memoryLink } from "./Approvals";
import { ApprovalCard as InlineApprovalCard } from "./Chat";
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

describe("a question in the chat", () => {
  const field = (name: string, label: string, options: string[]) =>
    ({ name, label, type: "text", value: "", editable: true, options });
  const asks = (fields: unknown[], options: string[] = []) =>
    renderToStaticMarkup(createElement(InlineApprovalCard, {
      card: { id: "q1", type: "question", task_id: "t1", agent: "general", title: "General - Questions",
              message: "Answer these together, then send them.", options, status: "pending", chosen_option: "",
              created_at: "", fields, context: "", response: {}, blocking: true, source: "" } as unknown as Approval,
      respond: async () => undefined,
    }));

  it("asks several questions one at a time, each option as a title with its explanation", () => {
    // They were all on one card as uneven pills, with a "Send answers" button that looked disabled.
    const html = asks([
      field("q1", "What form should this take?", ["North-native: flows that find jobs", "Prep help only"]),
      field("q2", "Which piece first?", ["Job discovery"]),
    ]);
    expect(html).toContain("Question 1 of 2");
    expect(html).toContain("What form should this take?");
    expect(html).toContain("<span>North-native</span><small>flows that find jobs</small>");
    expect(html).toContain("Something else…");
    expect(html).not.toContain("Which piece first?");
    expect(html).not.toContain("Send answers");
    expect(html).not.toContain("General - Questions");
  });

  it("takes a single question's answer from its options or in your own words", () => {
    const html = asks([], ["Q2", "Q3"]);
    expect(html).toContain("<span>Q2</span>");
    expect(html).toContain("Something else…");
    expect(asks([])).toContain('placeholder="Type your answer"');
  });
});

// Issue #76, A18: cards showed markdown fences raw ("```git checkout -- draft.txt```"), and a resolved
// row showed only its title and who decided, so there was nothing to judge an overrule by.
describe("what an approval card shows", () => {
  const card = (status: string, decided_by = "") =>
    ({ id: "a1", type: "approval", task_id: "t1", agent: "git", title: "Git Operation - Approval Required",
       message: "Run git?\n```\ngit checkout -- draft.txt\n```", options: ["Approve", "Reject"], status,
       chosen_option: "", created_at: "", fields: [], context: "", response: {}, blocking: true, source: "",
       decided_by, decided_by_label: decided_by ? "the safe list" : "", reason: "local-only git",
       memory_used: [] }) as unknown as Approval;

  it("renders the command as code on the approvals page and in the chat", () => {
    const page = renderToStaticMarkup(createElement(ApprovalCard, { card: card("pending"), onDecide: () => undefined }));
    const inline = renderToStaticMarkup(
      createElement(InlineApprovalCard, { card: card("pending"), respond: async () => undefined }),
    );
    for (const html of [page, inline]) {
      expect(html).toContain("<pre><code>git checkout -- draft.txt</code></pre>");
      expect(html).not.toContain("```");
    }
  });

  it("shows what a resolved card decided", () => {
    const html = renderToStaticMarkup(
      createElement(DecisionRow, { card: card("approved", "safe_list"), onOverrule: async () => undefined }),
    );
    expect(html).toContain("git checkout -- draft.txt");
  });
});
