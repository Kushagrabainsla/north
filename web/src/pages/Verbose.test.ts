import { describe, expect, it } from "vitest";
import { agentDeletionPrompt } from "./Verbose";

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
