import { MessageSquare } from "lucide-react";
import { formatDateTime, HealthIndicator, NoticeList, PageHeader, Panel, timeAgo, unpricedNote, YoloBadge } from "../components";
import { Button, CardGrid, Empty, EntityCard, ListRow, Loading, Notice, Stat, StatGrid, Status } from "../kit";
import { useResource } from "../hooks";
import type { DashboardData } from "../types";
import { SetupReminder } from "./Setup";

/** The approval mode. Models and power are on the Settings page only (#80): a copy here disagreed with it. */
export function dialsLine(system: DashboardData["system"]): string {
  return `Autonomy ${system.autonomy}`;
}

export function Dashboard() {
  const { data, error, loading, reload } = useResource<DashboardData>("/web/api/dashboard", 10000);
  // The header stays while the dashboard loads or fails, with Refresh in it.
  const header = <PageHeader title="Everything, at a glance"
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
    {/* Attention and Sessions take the whole width; the Active now, Memory and Artifacts tiles are gone:
      each repeats its own page, and Attention is what needs you. */}
    <div className="cockpit-grid">
      <Panel title="Attention" label={`${data.attention.length + data.notices.length} items`} to="/approvals" className={`full-panel${data.attention.length + data.notices.length ? " attention-panel" : ""}`}>
        <NoticeList compact notices={data.notices.slice(0, 3)} onDismissed={() => void reload()}/>
        {data.attention.length ? data.attention.slice(0, 3).map(item => <ListRow key={item.id} to="/approvals" title={item.title} meta={[`${item.agent} needs a decision`]}/>) : !data.notices.length && <Empty>Nothing needs you right now.</Empty>}
      </Panel>
      <Panel title="Sessions" label="Recent" to="/sessions" className="full-panel">
        {data.conversations.length ? <CardGrid>{data.conversations.map(session => <EntityCard key={session.id} to={`/sessions/${session.id}`} icon={<MessageSquare size={16}/>} title={session.title} meta={`${session.source === "cli" ? "CLI · " : ""}${timeAgo(session.updated_at)}`}/>)}</CardGrid> : <Empty>Start your first session.</Empty>}
      </Panel>
      <Panel title="System" label="Status" to="/system" className="system-panel">
        <div className="hero-status"><HealthIndicator variant="panel"/><div><p>{dialsLine(data.system)}<YoloBadge mode={data.system.autonomy}/></p></div></div>
      </Panel>
      <Panel title={todaysBriefing ? "Today's briefing" : "Latest briefing"} label="Daily intelligence" to="/artifacts" className="wide-panel briefing-panel">
        {briefing ? <ListRow title={briefing.name}
          description={todaysBriefing ? "Today's news briefing is ready to read." : `Last updated ${timeAgo(briefing.updated_at)}.`}
          actions={<Button variant="primary" size="sm" to="/artifacts">Open artifact</Button>}/> : <Empty>No briefing has been generated yet.</Empty>}
      </Panel>
      <Panel title="Flows" label={`${data.scheduled ?? data.cron.length} scheduled`} to="/flows" className="wide-panel">
        {data.jobs.length ? data.jobs.slice(0, 3).map(job => <ListRow key={job.job_id} title={job.task} meta={[formatDateTime(job.scheduled_at), job.late_note]} status={<Status value={job.status}/>}/>) : <Empty>No flow has run yet.</Empty>}
      </Panel>
      <Panel title="Usage" label="Last 7 days" to="/insights">
        <StatGrid><Stat label="Tasks" value={String(data.metrics.total_tasks || 0)}/><Stat label="Model cost" value={<>${totalCost.toFixed(3)}{unpricedNote(data.metrics.unpriced_calls)}</>}/></StatGrid>
      </Panel>
      {/* No "Latest events" panel: north's own background calls filled it, eight rows of "inference call ·
        memory · completed" that said nothing (issue #81). Every event is on the Tasks page. */}
    </div>
  </div>;
}
