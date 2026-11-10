import { useEffect, useRef } from "react";
import type { ChangeEvent, TextareaHTMLAttributes } from "react";
import { Badge, type Column } from "../kit";

// North runs every skill itself, so a skill's contract names what it may use,
// never who runs it: the API no longer sends an agent.
export interface SkillExecution {
  tools: string[];
  approval: "never" | "on_mutation" | "before_step";
  inputs: {
    properties?: Record<string, { type?: string; enum?: unknown[] }>;
    required?: string[];
  };
  outputs: {
    properties?: Record<string, { type?: string; enum?: unknown[] }>;
    required?: string[];
  };
  success_criteria: string[];
}
export interface SkillSummary {
  name: string;
  description: string;
  source: string;
  version: string;
  status: string;
  domains: string[];
  execution: SkillExecution | null;
}
export interface SkillDetail {
  name: string;
  content: string;
  source: string;
  execution: SkillExecution | null;
}
export function ContentTextarea({
  value,
  onChange,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  value: string;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const resize = () => {
    const element = ref.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${element.scrollHeight}px`;
  };
  useEffect(() => {
    resize();
  }, [value]);
  return (
    <textarea
      {...props}
      ref={ref}
      value={value}
      onChange={(event) => {
        onChange(event);
        requestAnimationFrame(resize);
      }}
    />
  );
}

export interface ToolSummary {
  name: string;
  description: string;
  source: string;
  status: string;
  mutating: boolean;
}
export interface ToolDetail extends ToolSummary {
  content: string;
}

/** How a skill runs, in the Skills list: a step of a flow, and how many tools it may use. */
export function skillRunsAs(execution: SkillExecution | null): string {
  if (!execution) return "instructions only";
  const count = execution.tools.length;
  return count ? `flow step · ${count} tool${count === 1 ? "" : "s"}` : "flow step";
}

// Skills, Tools and Flows are sibling lists, so they share one table shape:
// the thing, what it is for, one fact about it, and its state.
export const capabilityColumns = (thing: string, fact: string): Column[] => [
  { key: "name", label: thing, width: "minmax(0, 1fr)" },
  { key: "description", label: "Description", width: "minmax(0, 2fr)" },
  { key: "fact", label: fact, width: "minmax(0, 1fr)" },
  { key: "status", label: "Status", width: "112px" },
];

/** The first cell of a capability row: its name and where it came from. */
export function CapabilityName({ name, source }: { name: string; source: string }) {
  return <><b>{name}</b><Badge>{sourceLabel(source)}</Badge></>;
}

/** Where a skill, tool or flow came from, in plain words. */
export const sourceLabel = (source: string): string =>
  source === "builtin" ? "built-in" : source;

/** What a page says after an action: a result, or a failure in the error colour. */
export interface PageMessage {
  tone: "success" | "error" | "info";
  text: string;
}
export const failure = (error: unknown): PageMessage => ({
  tone: "error",
  text: error instanceof Error ? error.message : String(error),
});

