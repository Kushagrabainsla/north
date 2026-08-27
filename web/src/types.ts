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
  media_type: string;
  size?: number;
  updated_at?: number;
  content?: string;
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
