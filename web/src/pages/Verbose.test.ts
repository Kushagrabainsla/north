import { describe, expect, it } from "vitest";
import { agentDeletionPrompt, memoryLabel, memoryLink } from "./Verbose";

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
    expect(params(link).get("tab")).toBe("approvals");
    expect(params(link).get("find")).toBe("abc123");
  });

  it("opens the right document for rules and the profile", () => {
    expect(params(memoryLink({ kind: "judgement_rules", label: "Your judgement rules", text: "", ref: "" })).get("doc")).toBe("judgement_rules.md");
    expect(params(memoryLink({ kind: "profile", label: "Your profile", text: "", ref: "" })).get("doc")).toBe("user.md");
  });

  it("names what kind of memory it was, as the API words it", () => {
    expect(memoryLabel({ kind: "fact", label: "Fact", text: "likes tea", ref: "" })).toBe("Fact: likes tea");
    expect(memoryLabel({ kind: "judgement_rules", label: "Your judgement rules", text: "", ref: "" })).toBe("Your judgement rules");
  });
});
