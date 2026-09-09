export type Status = "pending" | "running" | "completed" | "failed" | "cancelled" | "paused" | "queued";

export interface Conversation {
  id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  updated_at: string;
  turns?: Turn[];
}

export interface LedgerEntry {
  id: string;
  timestamp: string;
  source: string;
  task_id?: string;
  run_id?: string;
  agent?: string;
  input?: string;
  action?: string;
  output?: string;
  tools_used?: string[];
  model_used?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  status?: string;
  duration_ms?: number;
  error_type?: string;
}

export interface AgentRun {
  run_id: string;
  agent: string;
  status: string;
  attempt: number;
  duration_ms?: number;
  output?: string;
  error?: string;
  models_used: string[];
  providers_used?: string[];
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  skills: { name: string; version: string }[];
}

export interface TaskDetail {
  task: { task_id: string; status: Status; created_at?: string };
  output?: string;
  entries?: LedgerEntry[];
  runs?: AgentRun[];
}

/** One of North's self-checks, captured live from the task stream. */
export interface Signal {
  event: string;
  agent?: string;
  tone: "warn" | "ok" | "info";
  text: string;
  details: string[];
  timestamp: string;
}

export interface Turn {
  id: string;
  conversation_id: string;
  position: number;
  prompt: string;
  task_id?: string;
  created_at: string;
  detail?: TaskDetail;
}

export interface Artifact {
  id: string;
  name: string;
  kind: string;
  /** Task this artifact came out of; empty for personal outputs (news, notes, wellness). */
  task?: string;
  media_type: string;
  size?: number;
  updated_at?: number;
  content?: string;
}

export interface CardField {
  name: string;
  label: string;
  type: "text" | "textarea" | "number" | "boolean" | "select" | "link";
  value: unknown;
  editable: boolean;
  options: string[];
}

export interface Approval {
  id: string;
  type: string;
  task_id: string;
  agent: string;
  title: string;
  message: string;
  options: string[];
  status: string;
  chosen_option: string;
  created_at: string;
  // Work North filled in and is handing over. Empty for a plain "may I?" card.
  fields: CardField[];
  // Read-only source material shown beside the fields.
  context: string;
  // The values as decided, once resolved.
  response: Record<string, unknown>;
  // Whether something is waiting on the answer. False for work North has
  // finished and left for you to decide whenever.
  blocking: boolean;
  // What produced a card that outlives the task that made it.
  source: string;
  // What deciding this card will cause, in words. Empty when nothing is
  // registered for its source. "Submits the application" and "saves a draft"
  // must not be identical-looking buttons.
  next_step?: string;
}

export interface DashboardData {
  system: { status: string; power: string; autonomy: string };
  attention: Approval[];
  active_tasks: { task_id: string; status: string; created_at: string }[];
  conversations: Conversation[];
  agents: { name: string; domain: string; model_pool: string }[];
  jobs: { job_id: string; agent: string; task: string; status: string; scheduled_at: string }[];
  cron: { name: string; agent: string; task: string; hour: number; minute: number; weekday?: number }[];
  metrics: Record<string, unknown>;
  activity: LedgerEntry[];
  artifacts: Artifact[];
}

// One endpoint a routing walk passed over, and why. `tried` separates the two
// kinds that "skipped" alone conflates: north called this one and it answered
// badly, or north never called it because it was already known to be blocked.
export interface RoutingSkip {
  model: string;
  provider: string;
  reason: string;
  tried?: boolean;
  status_code?: number | null;
  detail?: string;
  retry_after?: number | null;
}

export interface RoutingDecision {
  id: string;
  task_id?: string;
  part: string;
  requirements: Record<string, unknown> | null;
  considered: number;
  skipped: RoutingSkip[] | null;
  // Totals for the whole walk. `skipped` is capped when stored, so these are
  // what the counts must come from.
  endpoints: number;
  attempted: number;
  chosen_model?: string | null;
  chosen_provider?: string | null;
  outcome: string;
  created_at: string;
}
