import { MessageSquare } from "lucide-react";
import { formatDateTime, HealthIndicator, NoticeList, PageHeader, Panel, timeAgo, unpricedNote, YoloBadge } from "../components";
import { Button, CardGrid, Empty, EntityCard, ListRow, Loading, Notice, Stat, StatGrid, Status, type Tone } from "../kit";
import { useResource } from "../hooks";
import type { DashboardData } from "../types";
import { SetupReminder } from "./Setup";

/** The approval mode. Models and power are on the Settings page only (#80): a copy here disagreed with it. */
export function dialsLine(system: DashboardData["system"]): string {
  return `Autonomy ${system.autonomy}`;
}

/** The System tile's facts, one row each: what you would otherwise go and look up. */
export function systemRows(system: DashboardData["system"]): { key: string; title: string; detail: string; state: string; tone: Tone }[] {
  const models = (system.models ?? []).map(model => ({
    key: `model:${model.name}`, title: model.name, detail: model.detail, state: model.state,
    tone: (model.state === "ready" ? "good" : model.state === "failing" ? "bad" : "neutral") as Tone,
  }));
  const waiting = (system.waiting ?? []).map(wait => ({
    key: `wait:${wait.model}`, title: wait.model, detail: `until ${formatDateTime(wait.until)}${wait.reason ? ` · ${wait.reason}` : ""}`,
    state: "waiting", tone: "warn" as Tone,
  }));
  const browser = (system.browser ?? []).map(profile => ({
    key: `browser:${profile.name}`, title: `Browser: ${profile.name}`, detail: "",
    state: profile.connected ? "connected" : "not connected", tone: (profile.connected ? "good" : "neutral") as Tone,
  }));
  return [...models, ...waiting, ...browser];
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
  const background = data.background ?? [];
  return <div className="page dashboard-page">
    {header}
    {error && <Notice tone="error">{error}</Notice>}
    {/* One column, in this order: setting north up (until it is done), what needs you, your sessions, north's
      state with its usage, and what ran in the background. The Active now, Memory and Artifacts tiles are gone:
      each repeated its own page. */}
    <div className="cockpit-grid">
      <SetupReminder/>
      <Panel title="Attention" label={`${data.attention.length + data.notices.length} items`} to="/approvals" className={`full-panel${data.attention.length + data.notices.length ? " attention-panel" : ""}`}>
        <NoticeList compact notices={data.notices.slice(0, 3)} onDismissed={() => void reload()}/>
        {data.attention.length ? data.attention.slice(0, 3).map(item => <ListRow key={item.id} to="/approvals" title={item.title} meta={[`${item.agent} needs a decision`]}/>) : !data.notices.length && <Empty>Nothing needs you right now.</Empty>}
      </Panel>
      <Panel title="Sessions" label="Recent" to="/sessions" className="full-panel">
        {data.conversations.length ? <CardGrid>{data.conversations.map(session => <EntityCard key={session.id} to={`/sessions/${session.id}`} icon={<MessageSquare size={16}/>} title={session.title} meta={`${session.source === "cli" ? "CLI · " : ""}${timeAgo(session.updated_at)}`}/>)}</CardGrid> : <Empty>Start your first session.</Empty>}
      </Panel>
      {/* One System panel: north's state and, beside it, what it used in the last 7 days (was a Usage tile). */}
      <Panel title="System" label={data.system.version ? `North ${data.system.version}` : "Status"} to="/system" className="full-panel system-panel">
        <div className="hero-status"><HealthIndicator variant="panel"/><div><p>{dialsLine(data.system)}<YoloBadge mode={data.system.autonomy}/></p></div></div>
        {systemRows(data.system).map(row => <ListRow key={row.key} title={row.title} meta={row.detail ? [row.detail] : []} status={<Status value={row.state} tone={row.tone}/>}/>)}
        <StatGrid>
          <Stat label="Tasks, last 7 days" value={String(data.metrics.total_tasks || 0)}/>
          <Stat label="Model cost, last 7 days" value={<>${totalCost.toFixed(3)}{unpricedNote(data.metrics.unpriced_calls)}</>}/>
        </StatGrid>
      </Panel>
      {/* What north did while you were away: the last day's flow runs, your own and north's when they matter.
        It replaced a tile for one person's briefing flows, which vanilla north does not have. */}
      <Panel title="Background" label="Last 24 hours" to="/flows" className="full-panel">
        {background.length ? background.map(run => <ListRow key={run.run_id} title={run.flow}
          meta={[timeAgo(run.at), run.file_name].filter(Boolean)}
          description={run.status === "completed" ? undefined : run.error || undefined}
          status={<Status value={run.status}/>}
          actions={run.file_id ? <a className="kit-button kit-button-secondary kit-button-sm" href={`/web/api/artifacts/${encodeURIComponent(run.file_id)}/file`} target="_blank" rel="noreferrer"><span>Open</span></a> : undefined}/>)
          : <Empty>Nothing ran in the background in the last day.</Empty>}
      </Panel>
      {/* No Flows tile: a run that needs you is a card under Attention, and the Flows page lists the rest. */}
      {/* No "Latest events" panel: north's own background calls filled it, eight rows of "inference call ·
        memory · completed" that said nothing (issue #81). Every event is on the Tasks page. */}
    </div>
  </div>;
}
