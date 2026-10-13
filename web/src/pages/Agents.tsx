import { useState } from "react";
import { del } from "../api";
import { ErrorNotice, PageHeader } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";

// Only what the agent-deletion prompt counts: the queued work an agent owns.
export interface Job {
  job_id: string;
  agent: string;
}

export interface Cron {
  name: string;
  label: string;
  title: string;
  description: string;
  agent: string;
  task: string;
  flow?: string;
  hour: number;
  minute: number;
  interval_minutes?: number | null;
  anchor_epoch?: number | null;
  weekdays: number[];
  cadence: string;
  enabled: boolean;
  tz: string;
  schedule: string;
  next_run_local: string;
  next_run_epoch: number;
  source: string;
  modified: boolean;
}

export interface Agent {
  name: string;
  domain: string;
  accepts: string[];
  source: string;
  deletable: boolean;
}
export interface Confidence {
  agent: string;
  tool: string;
  confidence: number;
}
export function agentDeletionPrompt(
  agentName: string,
  scheduleTitles: string[],
  pendingJobCount: number,
): string {
  const scheduleList = scheduleTitles.length
    ? `\n\nSchedules also deleted:\n${scheduleTitles.map((title) => `• ${title}`).join("\n")}`
    : "";
  const jobNotice = pendingJobCount
    ? `\n\n${pendingJobCount} pending job${pendingJobCount === 1 ? "" : "s"} will also be cancelled.`
    : "";
  return `Delete personal agent '${agentName}'?${scheduleList}${jobNotice}`;
}

export function Agents() {
  const dialog = useDialog();
  const agents = useResource<Agent[]>("/orchestrator/agents");
  const confidence = useResource<Confidence[]>(
    "/orchestrator/tools/confidence",
  );
  const schedules = useResource<Cron[]>("/orchestrator/cron");
  const pendingJobs = useResource<Job[]>(
    "/orchestrator/jobs?status=pending&limit=1000",
  );
  const [message, setMessage] = useState("");
  const cascadeReady =
    !schedules.loading &&
    !pendingJobs.loading &&
    !schedules.error &&
    !pendingJobs.error;
  const remove = async (agent: Agent) => {
    const linkedSchedules = (schedules.data || []).filter(
      (item) => item.agent === agent.name,
    );
    const linkedJobs = (pendingJobs.data || []).filter(
      (item) => item.agent === agent.name,
    );
    if (
      !agent.deletable ||
      !cascadeReady ||
      !(await dialog.confirm(
        agentDeletionPrompt(
          agent.name,
          linkedSchedules.map((item) => item.title),
          linkedJobs.length,
        ),
        {
          title: "Delete personal agent?",
          confirmLabel: "Delete agent",
          danger: true,
        },
      ))
    )
      return;
    try {
      await del(`/orchestrator/agents/${encodeURIComponent(agent.name)}`);
      setMessage(`Deleted personal agent '${agent.name}' and its future work.`);
      await Promise.all([
        agents.reload(),
        schedules.reload(),
        pendingJobs.reload(),
      ]);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="page">
      <PageHeader
        eyebrow="Capabilities"
        title="Agents"
        subtitle="North's specialist team and the tools they trust."
      />
      {agents.error && <ErrorNotice message={agents.error} />}{" "}
      {schedules.error && <ErrorNotice message={schedules.error} />}{" "}
      {pendingJobs.error && <ErrorNotice message={pendingJobs.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      <div className="agent-grid">
        {agents.data?.map((agent) => (
          <article key={agent.name}>
            <div className="agent-avatar">
              {agent.name.slice(0, 1).toUpperCase()}
            </div>
            <h2>{agent.name}</h2>
            <p>
              {agent.domain} · {agent.source}
            </p>
            <div className="tag-list">
              {agent.accepts.slice(0, 5).map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
            <h3>Tool confidence</h3>
            {confidence.data
              ?.filter((item) => item.agent === agent.name)
              .slice(0, 4)
              .map((item) => (
                <div className="confidence" key={item.tool}>
                  <span>{item.tool}</span>
                  <i>
                    <b style={{ width: `${item.confidence * 100}%` }} />
                  </i>
                </div>
              ))}
            {agent.deletable && (
              <button
                className="danger-button"
                disabled={!cascadeReady}
                onClick={() => void remove(agent)}
              >
                Delete personal agent
              </button>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}

