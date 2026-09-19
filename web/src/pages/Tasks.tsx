// A task, and everything it did.
//
// This was two pages describing one thing. Tasks listed the runs and, on
// click, showed which models were tried. Activity was the same ledger with the
// task grouping taken off - so answering "what happened in this run?" meant
// opening Activity, filtering by hand for a task id copied from the other page,
// and reading events with no idea which belonged to which attempt.
//
// So: the list stays the way it was, and opening a task shows everything that
// task did - its agent runs, its full timeline, the models it tried, what it
// produced, and what it stopped to ask.
//
// The unscoped stream is still reachable, because not every event has a task.
// Startup, cron ticks and recovery sweeps belong to north rather than to any
// run, and dropping them would lose the only place they are visible.

import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { post } from "../api";
import { useResource } from "../hooks";
import { Empty, ErrorNotice, formatDateTime, InferenceCategories, Loading, PageHeader, Panel, PromptTelemetry, Status, timeAgo } from "../components";
import type { AgentRun, Approval, Artifact, Conversation, LedgerEntry, TaskDetail } from "../types";
import { RoutingAttempts, stageIcon } from "./Verbose";

/** One source of truth for the lifecycle actions shown in the task list. */
const TASK_ACTION_STATUS: Record<string, string> = {
  task_completed: "completed",
  task_completed_with_failures: "failed",
  task_failed: "failed",
  task_cancelled: "cancelled",
  task_stuck: "failed",
  task_skipped_model_unavailable: "failed",
  task_needs_attention: "needs_attention",
  task_queued: "queued",
  task_retrying: "retrying",
  task_paused: "paused",
};
const TERMINAL = new Set(["completed", "failed", "cancelled", "needs_attention"]);

export const isTerminal = (entry: LedgerEntry) =>
  Boolean(entry.action && TERMINAL.has(TASK_ACTION_STATUS[entry.action]));

const taskStatus = (rows: LedgerEntry[]) => {
  const lifecycle = rows.find(row => row.action && TASK_ACTION_STATUS[row.action]);
  return lifecycle?.action ? TASK_ACTION_STATUS[lifecycle.action] : "running";
};

export interface TaskSummary {
  id: string;
  prompt: string;
  agents: string[];
  status: string;
  latest: string;
  entries: LedgerEntry[];
}

/** The ledger, grouped back into the runs it came from. */
export function summarise(entries: LedgerEntry[]): TaskSummary[] {
  const byTask = new Map<string, LedgerEntry[]>();
  for (const entry of entries) {
    if (!entry.task_id) continue;
    byTask.set(entry.task_id, [...(byTask.get(entry.task_id) || []), entry]);
  }
  return [...byTask].map(([id, rows]) => ({
    id,
    // The ledger arrives newest first, so the prompt is at the far end.
    prompt: [...rows].reverse().find(row => row.action === "task_received")?.input || id,
    agents: [...new Set(rows.map(row => row.agent).filter(Boolean))] as string[],
    status: taskStatus(rows),
    latest: rows[0].timestamp,
    entries: rows,
  }));
}

// ── One event ────────────────────────────────────────────────────────────────

// `showTask` is on in the unscoped stream and off inside a task, where saying
// which task you are looking at for the fortieth time is noise. Activity showed
// it on every row because it had no other way to tell you - and then you had to
// copy the id to the other page by hand, which is what the merge is for, so here
// it is the link instead.
function EventRow({ entry, showTask = false }: { entry: LedgerEntry; showTask?: boolean }) {
  const body = entry.output || entry.input || "";
  return <div className="event-row">
    <span className={`event-dot ${entry.status || ""}`}/>
    <div>
      <b>{entry.action?.replaceAll("_", " ") || entry.source}</b>
      <small>
        {entry.agent || entry.source}
        {showTask && <> · {entry.task_id
          ? <Link className="event-task" to={`/tasks/${entry.task_id}`}>{entry.task_id}</Link>
          // north's own events - startup, cron ticks, recovery sweeps - belong to
          // no run. Activity called that "system"; keeping the word means the
          // column never reads as a missing value.
          : "system"}</>}
        {" · "}{formatDateTime(entry.timestamp)}
        {entry.model_used && ` · ${entry.model_used}`}
        {entry.duration_ms ? ` · ${(entry.duration_ms / 1000).toFixed(1)}s` : ""}
      </small>
      {body && <p>{body.slice(0, 600)}</p>}
    </div>
    <Status value={entry.status}/>
  </div>;
}

// ── Everything one task did ──────────────────────────────────────────────────

function RunRow({ run }: { run: AgentRun }) {
  return <div className="memory-row">
    <div className="memory-main">
      <b>{run.agent}{run.attempt > 1 && ` · attempt ${run.attempt}`}</b>
      <small>
        {run.models_used.join(", ") || "no model recorded"}
        {run.providers_used?.length ? ` · ${run.providers_used.join(", ")}` : ""}
        {run.duration_ms ? ` · ${(run.duration_ms / 1000).toFixed(1)}s` : ""}
        {` · ${(run.tokens_in + run.tokens_out).toLocaleString()} tokens`}
        {run.cost_usd ? ` · $${run.cost_usd.toFixed(4)}` : ""}
        {run.skills?.length ? ` · skills: ${run.skills.map(s => s.name).join(", ")}` : ""}
      </small>
      <PromptTelemetry run={run}/>
      {run.error && <p className="run-error">{run.error}</p>}
    </div>
    <Status value={run.status}/>
  </div>;
}

function TaskActivity({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const detail = useResource<TaskDetail>(`/web/api/tasks/${encodeURIComponent(taskId)}`, 5000);
  // Artifacts and approvals are whole-collection endpoints; filtering here beats
  // adding a per-task variant of each for a page that already has them cached.
  const artifacts = useResource<Artifact[]>("/web/api/artifacts", 30000);
  const approvals = useResource<Approval[]>("/web/api/approvals", 30000);

  if (detail.loading) return <Loading/>;
  if (detail.error) return <div className="page"><ErrorNotice message={detail.error}/></div>;

  const entries = detail.data?.entries || [];
  const runs = detail.data?.runs || [];
  const inferenceCategories = detail.data?.inference_categories || [];
  const prompt = entries.find(entry => entry.action === "task_received")?.input || taskId;
  const status = detail.data?.task?.status || taskStatus(entries);
  const mine = (artifacts.data || []).filter(file => file.task === taskId);
  const asked = (approvals.data || []).filter(card => card.task_id === taskId);
  const spend = inferenceCategories.length
    ? inferenceCategories.reduce((total, item) => total + item.cost_usd, 0)
    : runs.reduce((total, run) => total + (run.cost_usd || 0), 0);

  // A task that has not finished has no result yet. The API falls back to the
  // last entry carrying any output, which for a running task is whatever it
  // happened to log - a skill list, a classification - presented under a
  // heading that claims it is the answer.
  const finished = ["completed", "failed", "cancelled", "needs_attention"].includes(String(status));

  return <div className="page task-detail">
    <PageHeader eyebrow="Task" title={prompt}
      subtitle={`${taskId} · ${timeAgo(detail.data?.task?.created_at || entries[0]?.timestamp || "")}`}
      actions={<button className="task-back" onClick={() => navigate("/tasks")}>← All tasks</button>}/>

    <div className="metric-cards">
      <div><span>Status</span><strong className="task-status">{status}</strong></div>
      <div><span>Agent runs</span><strong>{runs.length}</strong></div>
      <div><span>Events</span><strong>{entries.length}</strong></div>
      <div><span>Cost</span><strong>${spend.toFixed(4)}</strong></div>
    </div>

    {finished && detail.data?.output && <Panel title="Result">
      <p className="task-output">{detail.data.output}</p>
    </Panel>}

    {!!runs.length && <Panel title="Agent runs" label={`${runs.length} in this task`}>
      {runs.map(run => <RunRow key={run.run_id} run={run}/>)}
    </Panel>}

    {!!inferenceCategories.length && <Panel title="Model calls" label="Separated by purpose">
      <InferenceCategories categories={inferenceCategories}/>
    </Panel>}

    {!!asked.length && <Panel title="What it stopped to ask" label={`${asked.length}`}>
      {asked.map(card => <div className="memory-row" key={card.id}>
        <div className="memory-main"><b>{card.title}</b><small>{card.message}</small></div>
        <Status value={card.status}/>
      </div>)}
    </Panel>}

    {!!mine.length && <Panel title="What it produced" label={`${mine.length} file${mine.length > 1 ? "s" : ""}`}>
      <div className="artifact-grid">
        {mine.map(file => <div className="artifact-card" key={file.id}>
          <span>{stageIcon[file.kind] || "◇"}</span>
          <div><b>{file.name}</b><small>{file.kind} · {timeAgo(file.updated_at)}</small></div>
        </div>)}
      </div>
    </Panel>}

    {/* The whole reason the pages were merged: this is the Activity stream,
        already scoped to the run you are looking at. */}
    <Panel title="Timeline" label={`${entries.length} events`}>
      {entries.length
        ? <div className="event-list verbose-events">{entries.map(entry => <EventRow key={entry.id} entry={entry}/>)}</div>
        : <Empty>Nothing was recorded for this task.</Empty>}
    </Panel>

    {/* Shown, not hidden behind a button. Which models a task tried is part of
        what it did, and the page exists to answer that in one place - putting it
        behind a click recreated the trip to another view the merge removed. It
        is one more request when a task is opened, next to the three already
        made, which is not a reason to make someone ask twice. */}
    <Panel title="Models tried" label="In walk order">
      <RoutingAttempts taskId={taskId}/>
    </Panel>
  </div>;
}

// ── The list ─────────────────────────────────────────────────────────────────

function AllEvents({ entries, query }: { entries: LedgerEntry[]; query: string }) {
  const rows = useMemo(
    () => entries.filter(entry => JSON.stringify(entry).toLowerCase().includes(query.toLowerCase())),
    [entries, query],
  );
  if (!rows.length) return <Empty>No events match.</Empty>;
  return <div className="event-list verbose-events">{rows.map(entry => <EventRow key={entry.id} entry={entry} showTask/>)}</div>;
}

export function Tasks() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=500", 7000);
  const [query, setQuery] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState<"refresh" | "create" | "">("");
  // The tab is in the URL so the old /activity path can land on the stream
  // rather than dropping someone on the task list and making them find it.
  const [params, setParams] = useSearchParams();
  type TaskView = "all" | "active" | "approvals" | "completed" | "everything";
  const requested = params.get("view");
  const tab: TaskView = ["active", "approvals", "completed", "everything"].includes(requested || "")
    ? requested as TaskView : "all";
  const showTab = (next: TaskView) => setParams(next === "all" ? {} : { view: next }, { replace: true });

  if (taskId) return <TaskActivity taskId={taskId}/>;
  if (resource.loading) return <Loading/>;

  const entries = resource.data || [];
  const tasks = summarise(entries);
  const matches = query.toLowerCase();
  const visibleByView = tasks.filter(task => {
    if (tab === "active") return ["running", "retrying", "paused"].includes(task.status);
    if (tab === "approvals") return task.status === "needs_attention";
    if (tab === "completed") return task.status === "completed";
    return true;
  });
  const shown = visibleByView.filter(task =>
    !matches || task.prompt.toLowerCase().includes(matches) || task.id.toLowerCase().includes(matches)
    || task.status.toLowerCase().includes(matches)
    || task.agents.some(agent => agent.toLowerCase().includes(matches)));
  const active = tasks.filter(task => ["running", "retrying", "paused"].includes(task.status)).length;
  const approvalsCount = tasks.filter(task => task.status === "needs_attention").length;
  const queued = tasks.filter(task => task.status === "queued").length;
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const completedThisWeek = tasks.filter(task => task.status === "completed" && new Date(task.latest).getTime() >= weekAgo).length;
  const refresh = async () => {
    setBusy("refresh"); setActionError("");
    try { await resource.reload(); }
    catch (error) { setActionError(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(""); }
  };
  const createTask = async () => {
    setBusy("create"); setActionError("");
    try {
      const chat = await post<Conversation>("/web/api/conversations", { title: "New task" });
      navigate(`/chat/${chat.id}`);
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)); setBusy(""); }
  };

  return <div className="page tasks-ledger-page">
    <PageHeader eyebrow="Work" title="Tasks"
      subtitle="Everything North is doing, waiting on, or has completed."
      actions={<><button disabled={Boolean(busy)} onClick={() => void refresh()}>{busy === "refresh" ? "Refreshing…" : "Refresh"}</button><button className="primary-button" disabled={Boolean(busy)} onClick={() => void createTask()}>{busy === "create" ? "Creating…" : "+ New task"}</button></>}/>
    {resource.error && <ErrorNotice message={resource.error}/>}
    {actionError && <ErrorNotice message={actionError}/>}

    <section className="task-ledger-stats" aria-label="Task summary">
      <div><small>Active</small><strong>{active}</strong><span>in progress</span></div>
      <div><small>Needs approval</small><strong>{approvalsCount}</strong><span>waiting on you</span></div>
      <div><small>Queued</small><strong>{queued}</strong><span>ready to start</span></div>
      <div><small>Completed this week</small><strong>{completedThisWeek}</strong><span>finished work</span></div>
    </section>

    {/* Most of the time you want the runs. The raw stream is still here because
        startup, cron ticks and recovery sweeps have no task to belong to. */}
    <div className="task-ledger-toolbar">
      <div className="task-ledger-tabs" role="tablist" aria-label="Task views">
        <button role="tab" aria-selected={tab === "all"} className={tab === "all" ? "active" : ""} onClick={() => showTab("all")}>All work</button>
        <button role="tab" aria-selected={tab === "active"} className={tab === "active" ? "active" : ""} onClick={() => showTab("active")}>Active</button>
        <button role="tab" aria-selected={tab === "approvals"} className={tab === "approvals" ? "active" : ""} onClick={() => showTab("approvals")}>Approvals</button>
        <button role="tab" aria-selected={tab === "completed"} className={tab === "completed" ? "active" : ""} onClick={() => showTab("completed")}>Completed</button>
        <button role="tab" aria-selected={tab === "everything"} className={tab === "everything" ? "active" : ""} onClick={() => showTab("everything")}>Everything</button>
      </div>
      <input className="task-ledger-search" aria-label={tab === "everything" ? "Filter events" : "Filter tasks"}
        placeholder={tab === "everything" ? "Filter events" : "Filter tasks"}
        value={query} onChange={event => setQuery(event.target.value)}/>
    </div>

    {tab !== "everything"
      ? <>
          <div className="task-ledger-table-wrap">
            <table className="task-ledger-table">
              <thead><tr><th>Task</th><th>Agent</th><th>Updated</th><th>Events</th><th>State</th><th><span className="sr-only">Action</span></th></tr></thead>
              <tbody>{shown.map(task => <tr key={task.id}>
                <td><Link className="task-ledger-primary" to={`/tasks/${task.id}`}><b>{task.prompt}</b><small>{task.id}</small></Link></td>
                <td data-label="Agent">{task.agents.join(", ") || "orchestrator"}</td>
                <td data-label="Updated">{timeAgo(task.latest)}</td>
                <td data-label="Events">{task.entries.length}</td>
                <td data-label="State"><Status value={task.status}/></td>
                <td><Link className="task-ledger-open" to={`/tasks/${task.id}`} aria-label={`Open task: ${task.prompt}`}>Open</Link></td>
              </tr>)}</tbody>
            </table>
          </div>
          {!shown.length && <Empty>{tasks.length ? "No tasks match this view." : "No task history yet."}</Empty>}
        </>
      : <AllEvents entries={entries} query={query}/>}
  </div>;
}
