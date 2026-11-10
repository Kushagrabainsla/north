import { MessageSquare, File } from "lucide-react";
import { formatDateTime, HealthIndicator, NoticeList, PageHeader, Panel, timeAgo, unpricedNote, YoloBadge } from "../components";
import { Button, CardGrid, Empty, EntityCard, ListRow, Loading, Notice, Stat, StatGrid, Status } from "../kit";
import { useResource } from "../hooks";
import type { DashboardData } from "../types";
import { SetupReminder } from "./Setup";
import { EventList } from "../events";

/** What decides the model: the pinned one under manual routing, where power is not in use, else power. */
export function dialsLine(system: DashboardData["system"]): string {
  // The model's own name; the provider prefix crowded the health label out of the tile.
  const pinned = system.routing === "manual" && system.model ? system.model.split(":").pop() : "";
  const dial = pinned ? `Model ${pinned} (pinned)` : `Power ${system.power}`;
  return `${dial} · ${system.autonomy}`;
}

/** What memory holds, counted. The tile said "4 context documents" over an empty memory. */
export function memoryLine(memory: DashboardData["memory"]): { count: number; label: string; note: string } {
  const facts = memory?.facts ?? 0;
  const documents = memory?.documents ?? 0;
  return {
    count: facts,
    label: facts === 1 ? "fact about you" : "facts about you",
    note: documents
      ? `${documents} of ${memory?.of_documents ?? 2} documents written: north stars and judgement rules.`
      : "No north stars or judgement rules written yet.",
  };
}

export function Dashboard() {
  const { data, error, loading, reload } = useResource<DashboardData>("/web/api/dashboard", 10000);
  // The header stays while the dashboard loads or fails, with Refresh in it.
  const header = <PageHeader title="Everything, at a glance" subtitle="The complete state of North. Live, local, and under your control."
    actions={<Button onClick={() => void reload()}>Refresh</Button>} />;
  if (!data) return <div className="page dashboard-page">
    {header}
    {loading ? <Loading/> : <Notice tone="error">{error || "Dashboard unavailable"}</Notice>}
  </div>;
  const totalCost = Number(data.metrics.total_cost_usd || 0);
  const briefing = data.artifacts.find(artifact => artifact.kind === "news");
  const todaysBriefing = briefing?.is_today === true;
  return <div className="page dashboard-page">
    {header}
    {error && <Notice tone="error">{error}</Notice>}
    <SetupReminder/>
    <div className="cockpit-grid">
      <Panel title="System" label="Status" to="/system" className="system-panel">
        <div className="hero-status"><HealthIndicator variant="panel"/><div><p>{dialsLine(data.system)}<YoloBadge mode={data.system.autonomy}/></p></div></div>
      </Panel>
      <Panel title="Attention" label={`${data.attention.length + data.notices.length} items`} to="/approvals" className={data.attention.length + data.notices.length ? "attention-panel" : ""}>
        <NoticeList compact notices={data.notices.slice(0, 3)} onDismissed={() => void reload()}/>
        {data.attention.length ? data.attention.slice(0, 3).map(item => <ListRow key={item.id} to="/approvals" title={item.title} meta={[`${item.agent} needs a decision`]}/>) : !data.notices.length && <Empty>Nothing needs you right now.</Empty>}
      </Panel>
      <Panel title="Active now" label={`${data.active_tasks.length} tasks`} to="/tasks">
        {data.active_tasks.length ? data.active_tasks.map(task => <ListRow key={task.task_id} title={task.task_id.slice(0, 12)} meta={[timeAgo(task.created_at)]} status={<Status value={task.status}/>}/>) : <Empty>North is ready.</Empty>}
      </Panel>
      <Panel title="Sessions" label="Recent" to="/sessions" className="wide-panel">
        {data.conversations.length ? <CardGrid>{data.conversations.map(session => <EntityCard key={session.id} to={`/sessions/${session.id}`} icon={<MessageSquare size={16}/>} title={session.title} meta={`${session.source === "cli" ? "CLI · " : ""}${timeAgo(session.updated_at)}`}/>)}</CardGrid> : <Empty>Start your first session.</Empty>}
      </Panel>
      <Panel title={todaysBriefing ? "Today's briefing" : "Latest briefing"} label="Daily intelligence" to="/artifacts" className="wide-panel briefing-panel">
        {briefing ? <ListRow title={briefing.name}
          description={todaysBriefing ? "Today's news briefing is ready to read." : `Last updated ${timeAgo(briefing.updated_at)}.`}
          actions={<Button variant="primary" size="sm" to="/artifacts">Open artifact</Button>}/> : <Empty>No briefing has been generated yet.</Empty>}
      </Panel>
      <Panel title="Flows" label={`${data.scheduled ?? data.cron.length} scheduled`} to="/flows">
        {data.jobs.length ? data.jobs.slice(0, 3).map(job => <ListRow key={job.job_id} title={job.task} meta={[formatDateTime(job.scheduled_at), job.late_note]} status={<Status value={job.status}/>}/>) : <Empty>No flow has run yet.</Empty>}
      </Panel>
      <Panel title="Artifacts" label={`${data.artifacts.length} recent`} to="/artifacts">
        {data.artifacts.length ? data.artifacts.slice(0, 4).map(file => <ListRow key={file.id} title={file.name} meta={[file.kind, timeAgo(file.updated_at)]} status={<File size={14} aria-hidden="true"/>}/>) : <Empty>No generated outputs.</Empty>}
      </Panel>
      <Panel title="Memory" label="Context" to="/memory">
        {(() => { const memory = memoryLine(data.memory); return <StatGrid><Stat label={memory.label} value={memory.count} detail={memory.note}/></StatGrid>; })()}
      </Panel>
      <Panel title="Usage" label="Last 7 days" to="/insights">
        <StatGrid><Stat label="Tasks" value={String(data.metrics.total_tasks || 0)}/><Stat label="Model cost" value={<>${totalCost.toFixed(3)}{unpricedNote(data.metrics.unpriced_calls)}</>}/></StatGrid>
      </Panel>
      <Panel title="Activity" label="Latest events" to="/tasks?view=everything" className="full-panel">
        {data.activity.length === 0 ? <Empty>Nothing has happened yet.</Empty> : <EventList entries={data.activity.slice(0, 8)} brief/>}
      </Panel>
    </div>
  </div>;
}
