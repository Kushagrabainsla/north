import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DecisionRow, memoryLabel, memoryLink } from "./Approvals";
import { ApprovalCard, CardFieldRow, orderOwnOptions } from "./approvalCard";
import type { Approval, CardField } from "../types";

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

  it("offers the options as rows, with your own words behind Something else…", () => {
    const html = markup(["Q2", "Q3"]);
    expect(html).toContain("<span>Q2</span></button>");
    expect(html).toContain("Something else…");
  });
});

describe("the approvals page's card, in the kit's order", () => {
  const approval = (options: string[], rejecting: string[] = []) =>
    ({ id: "a1", type: "approval", task_id: "t1", agent: "git", title: "Git Operation - Approval Required",
       message: "Run git?", options, rejecting, status: "pending", chosen_option: "", created_at: "", fields: [],
       context: "", response: {}, blocking: true, source: "" }) as unknown as Approval;
  const page = (card: Approval) => renderToStaticMarkup(createElement(ApprovalCard, { card, onDecide: () => undefined }));

  it("puts Reject first and Approve last, as the one main action, at the end like the chat's card", () => {
    // It was Approve first, at the start, with a red Reject: the reverse of the chat.
    const html = page(approval(["Approve", "Reject"]));
    expect(html).toContain("kit-action-bar-end");
    expect(html.indexOf("<span>Reject</span>")).toBeGreaterThan(-1);
    expect(html.indexOf("<span>Reject</span>")).toBeLessThan(html.indexOf("<span>Approve</span>"));
    expect(html).toContain('kit-button-primary kit-button-md"><span>Approve</span>');
    expect(html).not.toContain("kit-button-danger");
  });

  it("orders a card's own options the same way: the rejecting one first, the main one last", () => {
    expect(orderOwnOptions(["Resume", "Discard"], ["Discard"])).toEqual([
      { option: "Discard", rejects: true, primary: false },
      { option: "Resume", rejects: false, primary: true },
    ]);
    const html = page(approval(["Resume", "Discard"], ["Discard"]));
    expect(html.indexOf("<span>Discard</span>")).toBeLessThan(html.indexOf("<span>Resume</span>"));
    expect(html).toContain('kit-button-primary kit-button-md"><span>Resume</span>');
  });

  it("offers a question's suggested answers as buttons, outside a label", () => {
    // In a <label>, a click on the question picked the first option.
    const html = renderToStaticMarkup(createElement(CardFieldRow, {
      field: { name: "q", label: "Which folder?", type: "text", value: "", editable: true, options: ["~/Reading", "~/Notes"] } as unknown as CardField,
      value: "~/Notes",
      asking: true,
      onChange: () => undefined,
    }));
    expect(html).not.toContain("<label");
    expect(html).toContain("Which folder?");
    expect(html).toContain('class="kit-button kit-button-secondary kit-button-sm" aria-pressed="true"><span>~/Notes</span>');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('placeholder="Your answer"');
  });
});

describe("a question in the chat", () => {
  const field = (name: string, label: string, options: string[]) =>
    ({ name, label, type: "text", value: "", editable: true, options });
  const asks = (fields: unknown[], options: string[] = []) =>
    renderToStaticMarkup(createElement(ApprovalCard, {
      card: { id: "q1", type: "question", task_id: "t1", agent: "general", title: "General - Questions",
              message: "Answer these together, then send them.", options, status: "pending", chosen_option: "",
              created_at: "", fields, context: "", response: {}, blocking: true, source: "" } as unknown as Approval,
      onDecide: () => undefined,
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
    const page = renderToStaticMarkup(createElement(ApprovalCard, { card: card("pending"), onDecide: () => undefined, wide: true }));
    const inline = renderToStaticMarkup(createElement(ApprovalCard, { card: card("pending"), onDecide: () => undefined }));
    for (const html of [page, inline]) {
      expect(html).toContain("<pre><code>git checkout -- draft.txt</code></pre>");
      expect(html).not.toContain("```");
    }
  });

  it("offers Reject, then Approve as the primary action, in the chat", () => {
    const inline = renderToStaticMarkup(createElement(ApprovalCard, { card: card("pending"), onDecide: () => undefined }));
    expect(inline).toMatch(/<div class="kit-action-bar kit-action-bar-end">.*kit-button-secondary[^>]*><span>Reject<\/span>.*kit-button-primary[^>]*><span>Approve<\/span>/);
  });

  it("shows what a resolved card decided", () => {
    const html = renderToStaticMarkup(
      createElement(DecisionRow, { card: card("approved", "safe_list"), onOverrule: async () => undefined }),
    );
    expect(html).toContain("git checkout -- draft.txt");
  });

  it("shows a resolved card as a row with its decision as a status, and offers to overrule north", () => {
    const html = renderToStaticMarkup(
      createElement(DecisionRow, { card: card("rejected", "safe_list"), onOverrule: async () => undefined }),
    );
    expect(html).toContain('class="kit-row"');
    expect(html).toContain('kit-status kit-tone-bad">rejected</span>');
    expect(html).toContain("<span>Overrule</span>");
    const yours = renderToStaticMarkup(
      createElement(DecisionRow, { card: card("approved", "you"), onOverrule: async () => undefined }),
    );
    expect(yours).not.toContain("Overrule");
  });
});

describe("one card on the approvals page and in the chat", () => {
  const prepared = (options: string[], rejecting: string[] = []) =>
    ({ id: "a2", type: "approval", task_id: "t1", agent: "north", title: "Send the application?", message: "Ready to send.",
       options, rejecting, status: "pending", chosen_option: "", created_at: "", fields: [], context: "The job post",
       response: {}, blocking: false, source: "jobs" }) as unknown as Approval;
  const render = (card: Approval, wide = false) =>
    renderToStaticMarkup(createElement(ApprovalCard, { card, onDecide: () => undefined, wide }));

  it("puts prepared work's source beside it on the page, and behind Review source in the chat", () => {
    expect(render(prepared([]), true)).toContain("kit-waiting-split");
    const chat = render(prepared([]));
    expect(chat).not.toContain("kit-waiting-split");
    expect(chat).toContain("<summary>Review source</summary>");
    expect(chat).toContain("nothing is waiting on this");
  });

  it("acts on a card's own options in the chat too, not a plain Approve and Reject", () => {
    // The chat's own card showed Approve and Reject on a Resume/Discard card, which did nothing.
    const chat = render(prepared(["Resume", "Discard"], ["Discard"]));
    expect(chat).toContain("<span>Resume</span>");
    expect(chat).not.toContain("<span>Approve</span>");
  });
});
