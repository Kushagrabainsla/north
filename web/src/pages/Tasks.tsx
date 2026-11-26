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

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { post } from "../api";
import { useResource } from "../hooks";
import { InferenceCategories, PageHeader, Panel, PromptTelemetry, timeAgo } from "../components";
import { Button, Empty, EntityTable, ListRow, Loading, Notice, ResourceView, SearchField, Stat, StatGrid, Status, Tabs, Toolbar, type Column, type TabItem } from "../kit";
import type { AgentRun, Approval, Artifact, Conversation, LedgerEntry, TaskDetail } from "../types";
import { RoutingAttempts } from "./routing";
import { EventList } from "../events";
import { ArtifactGrid } from "./Artifacts";
import { turnStatus } from "./Chat";

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

export const taskStatus = (rows: LedgerEntry[]) => {
  const lifecycle = rows.find(row => row.action && TASK_ACTION_STATUS[row.action]);
  if (!lifecycle?.action) return "running";
  // Finished, but what was asked did not happen: the chat says BLOCKED, so the list does too.
  if (lifecycle.action === "task_completed" && lifecycle.error_type === "blocked") return "blocked";
  return TASK_ACTION_STATUS[lifecycle.action];
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
    // Only an agent's own rows name an agent: model calls ("agent", "planning"), cards ("kasa") and your
    // messages ("user") listed as agents beside "general" (issue #76, P16).
    agents: [...new Set(rows.filter(row => row.source === "agent").map(row => row.agent).filter(Boolean))] as string[],
    status: taskStatus(rows),
    latest: rows[0].timestamp,
    entries: rows,
  }));
}

// ── One event ────────────────────────────────────────────────────────────────


// ── Everything one task did ──────────────────────────────────────────────────

/** What one agent run used, as the facts under its name. */
export function runFacts(run: AgentRun): string {
  return [
    run.models_used.join(", ") || "no model recorded",
    run.providers_used?.length ? run.providers_used.join(", ") : "",
    run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : "",
    `${(run.tokens_in + run.tokens_out).toLocaleString()} tokens`,
    run.cost_usd ? `$${run.cost_usd.toFixed(4)}` : "",
    run.skills?.length ? `skills: ${run.skills.map(s => s.name).join(", ")}` : "",
  ].filter(Boolean).join(" · ");
}

function RunRow({ run }: { run: AgentRun }) {
  return <ListRow
    title={<>{run.agent}{run.attempt > 1 && ` · attempt ${run.attempt}`}</>}
    description={runFacts(run)}
    meta={run.prompt_profiles?.length ? [<PromptTelemetry key="telemetry" run={run}/>] : undefined}
    status={<Status value={run.status}/>}>
    {run.error && <Notice tone="error">{run.error}</Notice>}
  </ListRow>;
}

function TaskActivity({ taskId }: { taskId: string }) {
  const detail = useResource<TaskDetail>(`/web/api/tasks/${encodeURIComponent(taskId)}`, 5000);
  // Artifacts and approvals are whole-collection endpoints; filtering here beats
  // adding a per-task variant of each for a page that already has them cached.
  const artifacts = useResource<Artifact[]>("/web/api/artifacts", 30000);
  const approvals = useResource<Approval[]>("/web/api/approvals", 30000);
  const back = <Button to="/tasks">← All tasks</Button>;

  // The header stays while the task loads or fails, so the way back is always there.
  if (!detail.data) return <div className="page task-detail">
    <PageHeader title={taskId} actions={back}/>
    {detail.error ? <Notice tone="error">{detail.error}</Notice> : <Loading/>}
  </div>;

  const entries = detail.data?.entries || [];
  const runs = detail.data?.runs || [];
  const inferenceCategories = detail.data?.inference_categories || [];
  const prompt = entries.find(entry => entry.action === "task_received")?.input || taskId;
  const asked = (approvals.data || []).filter(card => card.task_id === taskId);
  // The same status the chat shows for this task, from the same record.
  const status = turnStatus(
    String(detail.data?.task?.status || taskStatus(entries)),
    asked.some(card => card.status === "pending"),
    detail.data?.outcome_status,
  );
  const mine = (artifacts.data || []).filter(file => file.task === taskId);
  const spend = inferenceCategories.length
    ? inferenceCategories.reduce((total, item) => total + item.cost_usd, 0)
    : runs.reduce((total, run) => total + (run.cost_usd || 0), 0);

  // A task that has not finished has no result yet. The API falls back to the
  // last entry carrying any output, which for a running task is whatever it
  // happened to log - a skill list, a classification - presented under a
  // heading that claims it is the answer.
  const finished = !["running", "queued", "retrying", "paused", "working", "waiting_for_approval"].includes(status);

  return <div className="page task-detail">
    <PageHeader title={prompt}
      subtitle={`${taskId} · ${timeAgo(detail.data?.task?.created_at || entries[0]?.timestamp || "")}`}
      actions={back}/>
    {detail.error && <Notice tone="error">{detail.error}</Notice>}

    <StatGrid>
      <Stat label="Status" value={<Status value={status}/>}/>
      <Stat label="Agent runs" value={runs.length}/>
      <Stat label="Events" value={entries.length}/>
      <Stat label="Cost" value={`$${spend.toFixed(4)}`}/>
    </StatGrid>

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
      {asked.map(card => <ListRow key={card.id} title={card.title} description={card.message} status={<Status value={card.status}/>}/>)}
    </Panel>}

    {!!mine.length && <Panel title="What it produced" label={`${mine.length} file${mine.length > 1 ? "s" : ""}`}>
      <ArtifactGrid files={mine}/>
    </Panel>}

    {/* The whole reason the pages were merged: this is the Activity stream,
        already scoped to the run you are looking at. */}
    <Panel title="Timeline" label={`${entries.length} events`}>
      {entries.length
        ? <EventList entries={entries}/>
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

function AllEvents({ entries }: { entries: LedgerEntry[] }) {
  if (!entries.length) return <Empty>No events match.</Empty>;
  return <EventList entries={entries} showTask/>;
}

// How many tasks the list loads at first, and how many more each "Show more" adds. The list asks the
// server for whole tasks: the latest 500 events were 16 of 421 tasks, about a day and a half.
export const TASK_PAGE = 100;
// A search goes to the server once typing pauses this long, not on every key.
const SEARCH_PAUSE_MS = 300;

/** Where the list's data comes from: whole tasks, or for "Everything" the raw stream; searched on the server. */
export function ledgerPath(view: string, searched: string, tasks: number): string {
  const q = encodeURIComponent(searched);
  if (view === "everything") return searched ? `/orchestrator/ledger/search?q=${q}&limit=200` : "/orchestrator/ledger?limit=500";
  return `/orchestrator/ledger?tasks=${tasks}${searched ? `&q=${q}` : ""}`;
}

/** A search answers with matches that each hold their event; the stream answers with events. */
function asEntries(data: Array<LedgerEntry | { entry: LedgerEntry }> | null): LedgerEntry[] {
  return (data || []).map(item => ("entry" in item ? item.entry : item));
}

export function Tasks() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [taskCount, setTaskCount] = useState(TASK_PAGE);
  useEffect(() => {
    const timer = window.setTimeout(() => setSearched(query.trim()), SEARCH_PAUSE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState<"refresh" | "create" | "">("");
  // The tab is in the URL so the old /activity path can land on the stream
  // rather than dropping someone on the task list and making them find it.
  const [params, setParams] = useSearchParams();
  const requested = params.get("view");
  const tab: TaskView = ["active", "queued", "completed", "everything"].includes(requested || "")
    ? requested as TaskView : "all";
  const showTab = (next: TaskView) => setParams(next === "all" ? {} : { view: next }, { replace: true });
  const resource = useResource<Array<LedgerEntry | { entry: LedgerEntry }>>(
    taskId ? null : ledgerPath(tab, searched, taskCount), 7000);

  if (taskId) return <TaskActivity taskId={taskId}/>;

  const entries = asEntries(resource.data);
  const tasks = summarise(entries);
  const shown = tasks.filter(task => {
    if (tab === "active") return ["running", "retrying", "paused"].includes(task.status);
    if (tab === "queued") return task.status === "queued";
    if (tab === "completed") return task.status === "completed";
    return true;
  });
  const more = tab !== "everything" && tasks.length >= taskCount;
  const active = tasks.filter(task => ["running", "retrying", "paused"].includes(task.status)).length;
  const approvalsCount = tasks.filter(task => task.status === "needs_attention").length;
  const queued = tasks.filter(task => task.status === "queued").length;
  const completed = tasks.filter(task => task.status === "completed").length;
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
    <PageHeader title="Tasks"
      subtitle="What North is doing and has done."
      actions={<>
        <Button disabled={Boolean(busy)} busy={busy === "refresh"} busyLabel="Refreshing…" onClick={() => void refresh()}>Refresh</Button>
        <Button variant="primary" disabled={Boolean(busy)} busy={busy === "create"} busyLabel="Creating…" onClick={() => void createTask()}>+ New task</Button>
      </>}/>
    {resource.error && resource.data && <Notice tone="error">{resource.error}</Notice>}
    {actionError && <Notice tone="error">{actionError}</Notice>}

    {/* Counts are filters: click one to see those tasks. What waits for you is in the Inbox, and
        usage over time is on System, so neither is repeated here. */}
    <div className="filter-chips" role="group" aria-label="Show tasks by state">
      <button type="button" className={tab === "active" ? "on" : ""} onClick={() => showTab(tab === "active" ? "all" : "active")}><b>{active}</b> active</button>
      <button type="button" className={tab === "queued" ? "on" : ""} onClick={() => showTab(tab === "queued" ? "all" : "queued")}><b>{queued}</b> queued</button>
      <button type="button" className={tab === "completed" ? "on" : ""} onClick={() => showTab(tab === "completed" ? "all" : "completed")}><b>{completed}</b> completed</button>
      {approvalsCount > 0 && <Link className="chip-link" to="/inbox">{approvalsCount} wait for you in the Inbox →</Link>}
    </div>

    {/* Most of the time you want the runs. The raw stream is still here because
        startup, cron ticks and recovery sweeps have no task to belong to. */}
    <Toolbar
      tabs={<Tabs label="Task views" items={TASK_VIEWS} value={tab} onChange={showTab}/>}
      search={<SearchField label={tab === "everything" ? "Filter events" : "Filter tasks"} value={query} onChange={setQuery}/>}/>

    <ResourceView resource={resource}>
      {() => tab !== "everything"
        ? <>
            <EntityTable label="Tasks" columns={TASK_COLUMNS} rows={shown} rowKey={task => task.id}
              to={task => `/tasks/${task.id}`} cell={taskCell}
              empty={searched ? "No task matches this search." : tasks.length ? "No tasks match this view." : "No task history yet."}/>
            {more && <Button onClick={() => setTaskCount(count => count + TASK_PAGE)}>Show {TASK_PAGE} more tasks</Button>}
          </>
        : <AllEvents entries={entries}/>}
    </ResourceView>
  </div>;
}

type TaskView = "all" | "active" | "queued" | "completed" | "everything";

const TASK_VIEWS: TabItem<TaskView>[] = [
  { value: "all", label: "All work" },
  { value: "active", label: "Active" },
  { value: "queued", label: "Queued" },
  { value: "completed", label: "Completed" },
  { value: "everything", label: "Everything" },
];

const TASK_COLUMNS: Column[] = [
  { key: "task", label: "Task", width: "minmax(0, 2.4fr)" },
  { key: "agent", label: "Agent" },
  { key: "updated", label: "Updated", width: "minmax(0, .7fr)" },
  { key: "events", label: "Events", width: "minmax(0, .5fr)" },
  { key: "state", label: "State", width: "minmax(0, .8fr)" },
];

function taskCell(task: TaskSummary, key: string) {
  if (key === "task") return <><b>{taskTitle(task)}</b><small>{task.id}</small></>;
  if (key === "agent") return task.agents.join(", ") || "orchestrator";
  if (key === "updated") return timeAgo(task.latest);
  if (key === "events") return task.entries.length;
  return <Status value={task.status}/>;
}

/** A task's name. A task with no prompt (north's own work) showed its id twice: as the title and under it. */
export function taskTitle(task: { prompt: string; id: string }): string {
  if (task.prompt && task.prompt !== task.id) return task.prompt;
  if (task.id.startsWith("flow_")) return "Flow run";
  return "Background task";
}
