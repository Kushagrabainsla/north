// Tasks and Activity, merged.
//
// They were two pages describing one thing. Tasks listed the runs and, on
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
import { Link, useNavigate, useParams } from "react-router-dom";
import { useResource } from "../hooks";
import { Empty, ErrorNotice, Loading, PageHeader, Panel, Status, timeAgo } from "../components";
import type { AgentRun, Approval, Artifact, LedgerEntry, TaskDetail } from "../types";
import { RoutingAttempts, stageIcon } from "./Verbose";

/** Actions that end a task. Anything else leaves it running. */
const TERMINAL = ["task_completed", "task_failed", "task_cancelled"];

export const isTerminal = (entry: LedgerEntry) =>
  Boolean(entry.action && (entry.action.startsWith("task_completed") || TERMINAL.includes(entry.action)));

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
    status: rows.find(isTerminal)?.status || "running",
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
          ? <Link className="event-task" to={`/work/${entry.task_id}`}>{entry.task_id}</Link>
          // north's own events - startup, cron ticks, recovery sweeps - belong to
          // no run. Activity called that "system"; keeping the word means the
          // column never reads as a missing value.
          : "system"}</>}
        {" · "}{new Date(entry.timestamp).toLocaleString()}
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
  const [showRouting, setShowRouting] = useState(false);

  if (detail.loading) return <Loading/>;
  if (detail.error) return <div className="page"><ErrorNotice message={detail.error}/></div>;

  const entries = detail.data?.entries || [];
  const runs = detail.data?.runs || [];
  const prompt = entries.find(entry => entry.action === "task_received")?.input || taskId;
  const status = detail.data?.task?.status || entries.find(isTerminal)?.status || "running";
  const mine = (artifacts.data || []).filter(file => file.task === taskId);
  const asked = (approvals.data || []).filter(card => card.task_id === taskId);
  const spend = runs.reduce((total, run) => total + (run.cost_usd || 0), 0);

  // A task that has not finished has no result yet. The API falls back to the
  // last entry carrying any output, which for a running task is whatever it
  // happened to log - a skill list, a classification - presented under a
  // heading that claims it is the answer.
  const finished = ["completed", "failed", "cancelled"].includes(String(status));

  return <div className="page work-detail">
    <PageHeader eyebrow="Task" title={prompt}
      subtitle={`${taskId} · ${timeAgo(detail.data?.task?.created_at || entries[0]?.timestamp || "")}`}
      actions={<button className="work-back" onClick={() => navigate("/work")}>← All tasks</button>}/>

    <div className="metric-cards">
      <div><span>Status</span><strong className="work-status">{status}</strong></div>
      <div><span>Agent runs</span><strong>{runs.length}</strong></div>
      <div><span>Events</span><strong>{entries.length}</strong></div>
      <div><span>Cost</span><strong>${spend.toFixed(4)}</strong></div>
    </div>

    {finished && detail.data?.output && <Panel title="Result">
      <p className="work-output">{detail.data.output}</p>
    </Panel>}

    {!!runs.length && <Panel title="Agent runs" label={`${runs.length} in this task`}>
      {runs.map(run => <RunRow key={run.run_id} run={run}/>)}
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

    {/* Its own request, so it is fetched only when asked for. */}
    <Panel title="Models tried"
      actions={<button className="ghost-button" onClick={() => setShowRouting(!showRouting)}>
        {showRouting ? "Hide" : "Show"}
      </button>}>
      {showRouting ? <RoutingAttempts taskId={taskId}/> : <Empty>Every endpoint this task called or skipped.</Empty>}
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

export function Work() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=500", 7000);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"tasks" | "everything">("tasks");

  if (taskId) return <TaskActivity taskId={taskId}/>;
  if (resource.loading) return <Loading/>;

  const entries = resource.data || [];
  const tasks = summarise(entries);
  const matches = query.toLowerCase();
  const shown = tasks.filter(task =>
    !matches || task.prompt.toLowerCase().includes(matches) || task.id.toLowerCase().includes(matches)
    || task.agents.some(agent => agent.toLowerCase().includes(matches)));

  return <div className="page">
    <PageHeader eyebrow="Work" title="Tasks"
      subtitle="Every task, from prompt to final outcome - open one to see everything it did."
      actions={<input className="header-search" placeholder={tab === "tasks" ? "Filter tasks" : "Filter events"}
        value={query} onChange={event => setQuery(event.target.value)}/>}/>
    {resource.error && <ErrorNotice message={resource.error}/>}

    {/* Most of the time you want the runs. The raw stream is still here because
        startup, cron ticks and recovery sweeps have no task to belong to. */}
    <div className="segmented work-tabs">
      <button className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}>Tasks</button>
      <button className={tab === "everything" ? "active" : ""} onClick={() => setTab("everything")}>Everything</button>
    </div>

    {tab === "tasks"
      ? <>
          <div className="table-list">
            {shown.map(task => <div className="table-row task-row" key={task.id} onClick={() => navigate(`/work/${task.id}`)}>
              <div className="row-main">
                <b>{task.prompt}</b>
                <small>{task.id} · {timeAgo(task.latest)} · {task.entries.length} events</small>
              </div>
              <span>{task.agents.join(", ") || "orchestrator"}</span>
              <Status value={task.status}/>
            </div>)}
          </div>
          {!shown.length && <Empty>{tasks.length ? "No tasks match." : "No task history yet."}</Empty>}
        </>
      : <AllEvents entries={entries} query={query}/>}
  </div>;
}
