/** The live step list of a running task, folded from its event stream, in plain words.
 *
 *  The same events the TUI and `north run` render as a live table: which agent is running
 *  and on what model, each tool call and its result, the plan, and what the task waits on.
 *  Pure, so it is testable without a browser; the chat page feeds it one event at a time. */

export interface Step {
  icon: string;
  text: string;
  /** The step still in progress; a later event settles it. */
  active: boolean;
  /** The tool a call step is for, so its result settles that step instead of adding one. */
  tool?: string;
}

const MAX_STEPS = 80;
const PARAM_CHARS = 40;
const RESULT_CHARS = 50;
const THOUGHT_CHARS = 400;
/** Server-filled parameters every call carries; noise in a one-line preview. */
const HIDDEN_PARAMS = new Set(["workspace", "task_id"]);
/** Tools whose call is a card on the page: the card is the step, so the call is not listed. */
const CARD_TOOLS = new Set(["ask_user"]);

const LABELS: Record<string, [string, string]> = {
  north_star_checking: ["●", "Checking it against your goals"],
  north_star_aligned: ["✓", "Fits your goals"],
  north_star_conflict: ["▲", "Goes against one of your goals"],
  executing: ["●", "Working"],
  waiting_for_model: ["○", "Waiting for a model"],
  task_queued: ["○", "Waiting for a model to be free"],
  task_resumed: ["↻", "Resumed"],
  approval_required: ["?", "Waiting for your OK"],
  question_required: ["?", "Waiting for your answer"],
  approval_responded: ["✓", "Got your answer"],
  user_message: ["→", "Your message went into the task"],
  user_message_read: ["✓", "Read your message"],
  task_synthesis: ["✓", "Combined the agents' answers"],
  compaction: ["·", "Shortened the conversation"],
};

/** Every event the fold reacts to; the page subscribes to exactly these. */
export const STEP_EVENTS = [
  ...Object.keys(LABELS),
  "agent_started", "model", "tool_called", "tool_result", "agent_completed", "task_failed",
];

const truncate = (value: string, max: number) => (value.length > max ? `${value.slice(0, max - 1)}…` : value);
const oneLine = (value: string) => value.trim().replace(/\s+/g, " ");

/** "web_search" → "Web search". */
export const toolName = (tool: string) => {
  const words = tool.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Tool";
};

/** The one value that says what a call is about: its first short text parameter. */
function paramPreview(params: unknown): string {
  if (!params || typeof params !== "object") return "";
  const value = Object.entries(params as Record<string, unknown>)
    .find(([key, v]) => !key.startsWith("_") && !HIDDEN_PARAMS.has(key) && typeof v === "string" && v.trim())?.[1];
  return typeof value === "string" ? `: ${truncate(oneLine(value), PARAM_CHARS)}` : "";
}

function resultPreview(data: Record<string, any>): string {
  const text = oneLine(String(data.formatted || data.summary || data.error || ""));
  const said = text ? truncate(text, RESULT_CHARS) : "";
  if (data.success === false) return ` → failed${said ? `: ${said}` : ""}`;
  return said ? ` → ${said}` : "";
}

const settled = (steps: Step[]) => steps.map(step => (step.active ? { ...step, active: false } : step));

/** The step list after *event*; the input is not changed. */
export function foldStep(steps: Step[], event: string, data: Record<string, any> = {}): Step[] {
  const branch = data.parent_run_id ? "↳ " : "";
  const push = (icon: string, text: string, active = true, tool?: string) =>
    [...settled(steps), { icon, text, active, ...(tool ? { tool } : {}) }].slice(-MAX_STEPS);
  switch (event) {
    case "agent_started":
      // North's own run; a coding agent it starts shows as a branch.
      return push("◎", branch ? `↳ ${data.agent || "A coding agent"} started` : "Started");
    case "model": {
      // Names the model on the running agent's step rather than adding a step.
      const index = steps.map(s => s.icon === "◎" && s.active).lastIndexOf(true);
      if (index < 0 || !data.model) return steps;
      return steps.map((s, i) => (i === index ? { ...s, text: `${s.text} on ${data.model}` } : s));
    }
    case "tool_called": {
      const tool = String(data.tool || "tool");
      if (CARD_TOOLS.has(tool)) return steps;
      return push("⚙", `${branch}${toolName(tool)}${paramPreview(data.params || data.args)}`, true, tool);
    }
    case "tool_result": {
      const tool = String(data.tool || "tool");
      if (CARD_TOOLS.has(tool)) return steps;
      const icon = data.success === false ? "✗" : "✓";
      // The result settles its call's step: one line per call, as it ended.
      const index = steps.map(s => s.active && s.tool === tool).lastIndexOf(true);
      if (index < 0) return push(icon, `${branch}${toolName(tool)}${resultPreview(data)}`, false, tool);
      return steps.map((s, i) => (i === index ? { ...s, icon, text: `${s.text}${resultPreview(data)}`, active: false } : s));
    }
    case "agent_completed":
      return push("✓", branch ? `↳ ${data.agent || "The coding agent"} finished` : "Finished", false);
    case "task_failed":
      return steps.length ? steps.map((s, i) => (i === steps.length - 1 ? { ...s, icon: "✗", active: false } : s)) : steps;
    default: {
      const label = LABELS[event];
      if (!label) return steps;
      const waits = event.endsWith("_required") || label[0] === "●" || label[0] === "○";
      return push(label[0], label[1], waits);
    }
  }
}

/** What North is doing now: the step in progress, else the last one. */
export function currentActivity(steps: Step[]): string {
  const step = [...steps].reverse().find(s => s.active) || steps[steps.length - 1];
  return step ? step.text.replace(/^↳ /, "") : "";
}

/** North's newest thought: the last paragraph of what it has reasoned so far, on one line.
 *  A long one keeps its end, the part just written. */
export function latestThought(reasoning: string): string {
  const paragraphs = reasoning.split(/\n\s*\n/).map(oneLine).filter(Boolean);
  const thought = paragraphs[paragraphs.length - 1] || "";
  if (thought.length <= THOUGHT_CHARS) return thought;
  const tail = thought.slice(-THOUGHT_CHARS);
  return `…${tail.slice(tail.indexOf(" ") + 1)}`;
}

/** How long, in words: 24000 → "24s", 192000 → "3m 12s", 3725000 → "1h 2m". */
export function spoken(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor(total / 60) % 60}m`;
}

/** 24000 → "0:24"; an hour or more → "1:02:05". */
export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const [hours, minutes, seconds] = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}
