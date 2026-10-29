/** The live step list of a running task, folded from its event stream.
 *
 *  The same events the TUI and `north run` render as a live table: which agent is running
 *  and on what model, each tool call and its result, the plan, and what the task waits on.
 *  Pure, so it is testable without a browser; the chat page feeds it one event at a time. */

export interface Step {
  icon: string;
  text: string;
  /** The step still in progress; a later event settles it. */
  active: boolean;
}

const MAX_STEPS = 80;
const PARAM_CHARS = 28;
const RESULT_CHARS = 60;
/** Server-filled parameters every call carries; noise in a one-line preview. */
const HIDDEN_PARAMS = new Set(["workspace", "task_id"]);

const LABELS: Record<string, [string, string]> = {
  classifying: ["●", "classifying…"],
  classified_as_trivial: ["✓", "quick task"],
  north_star_checking: ["●", "checking goals…"],
  north_star_aligned: ["✓", "goals aligned"],
  north_star_conflict: ["▲", "goal conflict"],
  routing: ["●", "planning…"],
  executing: ["●", "running…"],
  waiting_for_model: ["○", "waiting for a model…"],
  task_queued: ["○", "queued, waiting for a model…"],
  task_resumed: ["↻", "resumed"],
  approval_required: ["?", "waiting for your approval"],
  question_required: ["?", "waiting for your answer"],
  approval_responded: ["✓", "answered"],
  user_message: ["→", "your message went into the task"],
  user_message_read: ["✓", "read your message"],
  task_synthesis: ["✓", "combined the agents' answers"],
  compaction: ["·", "compacted the conversation"],
};

/** Every event the fold reacts to; the page subscribes to exactly these. */
export const STEP_EVENTS = [
  ...Object.keys(LABELS),
  "classified", "routed", "agent_started", "model", "tool_called", "tool_result", "agent_completed", "task_failed",
];

const truncate = (value: string, max: number) => (value.length > max ? `${value.slice(0, max - 1)}…` : value);

function paramsPreview(params: unknown): string {
  if (!params || typeof params !== "object") return "";
  const pairs = Object.entries(params as Record<string, unknown>)
    .filter(([key]) => !key.startsWith("_") && !HIDDEN_PARAMS.has(key))
    .map(([key, value]) => `${key}=${truncate(typeof value === "string" ? value : JSON.stringify(value), PARAM_CHARS)}`);
  return pairs.length ? ` (${pairs.join(", ")})` : "";
}

function resultPreview(data: Record<string, any>): string {
  const text = String(data.formatted || data.summary || data.error || "").trim().replace(/\s+/g, " ");
  return text ? ` → ${truncate(text, RESULT_CHARS)}` : "";
}

const settled = (steps: Step[]) => steps.map(step => (step.active ? { ...step, active: false } : step));

/** The step list after *event*; the input is not changed. */
export function foldStep(steps: Step[], event: string, data: Record<string, any> = {}): Step[] {
  const branch = data.parent_run_id ? "↳ " : "";
  const push = (icon: string, text: string, active = true) => [...settled(steps), { icon, text, active }].slice(-MAX_STEPS);
  switch (event) {
    case "agent_started":
      return push("◎", `${branch}${data.agent || "agent"} agent running…`);
    case "model": {
      // Names the model on the running agent's step rather than adding a step.
      const index = steps.map(s => s.icon === "◎" && s.active).lastIndexOf(true);
      if (index < 0 || !data.model) return steps;
      const agent = steps[index].text.replace(/^↳ /, "").split(" ")[0];
      return steps.map((s, i) => (i === index ? { ...s, text: `${branch}${agent} running on ${data.model}…` } : s));
    }
    case "tool_called":
      return push("⚙", `${branch}${data.tool || "tool"}${paramsPreview(data.params || data.args)}…`);
    case "tool_result":
      return push(data.success === false ? "✗" : "✓", `${branch}${data.tool || "tool"}${resultPreview(data)}`, false);
    case "agent_completed":
      return push("✓", `${branch}${data.agent || "agent"}${data.summary ? `: ${data.summary}` : " agent done"}`, false);
    case "classified": {
      const shape = data.is_consequential ? "complex" : "direct";
      return push("✓", data.domain ? `classified: ${data.domain} (${shape})` : "classified", false);
    }
    case "routed": {
      const agents = (data.agents || []).join(", ");
      return push("✓", agents ? `plan ready: ${agents}` : "plan ready", false);
    }
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
