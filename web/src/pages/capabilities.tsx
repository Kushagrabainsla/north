import { useEffect, useRef } from "react";
import type { ChangeEvent, TextareaHTMLAttributes } from "react";

export interface SkillExecution {
  agent: string;
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

