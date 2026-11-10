import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CapabilityName, capabilityColumns, skillRunsAs, sourceLabel } from "./capabilities";
import type { SkillExecution } from "./capabilities";

const contract = (tools: string[]): SkillExecution => ({
  tools,
  approval: "never",
  inputs: {},
  outputs: {},
  success_criteria: ["Done"],
});

describe("how a skill runs, in the Skills list", () => {
  // The API stopped sending an agent once north ran every skill, and the
  // column read "undefined · 2 tools".
  it("says it is a flow step and counts its tools, never naming an agent", () => {
    expect(skillRunsAs(contract(["read_file", "web_search"]))).toBe("flow step · 2 tools");
    expect(skillRunsAs(contract(["read_file"]))).toBe("flow step · 1 tool");
    expect(skillRunsAs(contract(["read_file"]))).not.toContain("undefined");
  });

  it("says plain 'flow step' when the skill uses no tools", () => {
    expect(skillRunsAs(contract([]))).toBe("flow step");
  });

  it("says a skill without a contract is instructions only", () => {
    expect(skillRunsAs(null)).toBe("instructions only");
  });
});

describe("the sibling Skills, Tools and Flows tables", () => {
  it("share one column shape, differing only in the names", () => {
    const shape = (columns: ReturnType<typeof capabilityColumns>) => columns.map(({ key, width }) => [key, width]);
    expect(shape(capabilityColumns("Skill", "Runs as"))).toEqual(shape(capabilityColumns("Flow", "Schedule")));
    expect(capabilityColumns("Tool", "Operation").map(column => column.label)).toEqual(["Tool", "Description", "Operation", "Status"]);
  });

  it("names where a thing came from only when it is not built in", () => {
    // "built-in" on 48 of 48 skills said nothing.
    expect(renderToStaticMarkup(createElement(CapabilityName, { name: "daily-brief", source: "builtin" }))).toBe("<b>daily-brief</b>");
    expect(renderToStaticMarkup(createElement(CapabilityName, { name: "mine", source: "learned" }))).toBe('<b>mine</b><span class="kit-badge">learned</span>');
    expect(sourceLabel("learned")).toBe("learned");
  });
});
