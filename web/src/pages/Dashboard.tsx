import { NavLink } from "react-router-dom";
import { Empty, ErrorNotice, Loading, PageHeader, Panel, Status, timeAgo } from "../components";
import { useResource } from "../hooks";
import type { DashboardData } from "../types";

export function Dashboard() {
  const { data, error, loading, reload } = useResource<DashboardData>("/web/api/dashboard", 10000);
  if (loading) return <Loading />;
  if (error || !data) return <ErrorNotice message={error || "Dashboard unavailable"} />;
  const totalCost = Number(data.metrics.total_cost_usd || 0);
  return <div className="page dashboard-page">
    <PageHeader eyebrow="Live cockpit" title="Everything, at a glance" subtitle="The complete state of North. Live, local, and under your control."
      actions={<button className="ghost-button" onClick={reload}>Refresh</button>} />
    <div className="cockpit-grid">
      <Panel title="System" label="Status" to="/system" className="system-panel">
        <div className="hero-status"><span className="online-orb"/><div><strong>All systems operational</strong><p>Power {data.system.power} · {data.system.autonomy}</p></div></div>
      </Panel>
      <Panel title="Attention" label={`${data.attention.length} items`} to="/approvals" className={data.attention.length ? "attention-panel" : ""}>
        {data.attention.length ? data.attention.slice(0, 3).map(item => <NavLink className="list-row" to="/approvals" key={item.id}><div><b>{item.title}</b><small>{item.agent} needs a decision</small></div><span>→</span></NavLink>) : <Empty>Nothing needs you right now.</Empty>}
      </Panel>
      <Panel title="Active now" label={`${data.active_tasks.length} tasks`} to="/tasks">
        {data.active_tasks.length ? data.active_tasks.map(task => <div className="list-row" key={task.task_id}><div><b>{task.task_id.slice(0, 12)}</b><small>{timeAgo(task.created_at)}</small></div><Status value={task.status}/></div>) : <Empty>North is ready.</Empty>}
      </Panel>
      <Panel title="Conversations" label="Recent" to="/chat" className="wide-panel">
        {data.conversations.length ? <div className="conversation-strip">{data.conversations.map(chat => <NavLink to={`/chat/${chat.id}`} className="conversation-card" key={chat.id}><span className="conversation-glyph">◫</span><b>{chat.title}</b><small>{timeAgo(chat.updated_at)}</small></NavLink>)}</div> : <Empty>Start your first conversation.</Empty>}
      </Panel>
      <Panel title="Agents" label={`${data.agents.length} available`} to="/agents">
        <div className="agent-cloud">{data.agents.slice(0, 8).map(agent => <span key={agent.name}><i/>{agent.name}</span>)}</div>
      </Panel>
      <Panel title="Today's briefing" label="Daily intelligence" to="/artifacts" className="wide-panel briefing-panel">
        {data.artifacts.find(a => a.kind === "news") ? <div className="briefing-summary"><div className="sun-mark">☼</div><div><b>{data.artifacts.find(a => a.kind === "news")!.name}</b><p>Your latest news briefing is ready to read.</p></div><NavLink to="/artifacts" className="primary-button">Open artifact</NavLink></div> : <Empty>No briefing has been generated yet.</Empty>}
      </Panel>
      <Panel title="Schedule" label={`${data.cron.length} recurring`} to="/schedule">
        {data.jobs.length ? data.jobs.slice(0, 3).map(job => <div className="list-row" key={job.job_id}><div><b>{job.task}</b><small>{new Date(job.scheduled_at).toLocaleString()}</small></div><Status value={job.status}/></div>) : <Empty>No upcoming queued work.</Empty>}
      </Panel>
      <Panel title="Artifacts" label={`${data.artifacts.length} recent`} to="/artifacts">
        {data.artifacts.length ? data.artifacts.slice(0, 4).map(file => <div className="list-row compact" key={file.id}><div><b>{file.name}</b><small>{file.kind} · {timeAgo(file.updated_at)}</small></div><span>◇</span></div>) : <Empty>No generated outputs.</Empty>}
      </Panel>
      <Panel title="Memory" label="Context" to="/memory">
        <div className="metric"><strong>4</strong><span>context documents</span></div><p className="muted">Identity, goals, preferences, and judgement rules are available to North.</p>
      </Panel>
      <Panel title="Usage" label="Last 7 days" to="/insights">
        <div className="metric-row"><div className="metric"><strong>{String(data.metrics.total_tasks || 0)}</strong><span>tasks</span></div><div className="metric"><strong>${totalCost.toFixed(3)}</strong><span>model cost</span></div></div>
      </Panel>
      <Panel title="Activity" label="Latest events" to="/activity" className="full-panel">
        <div className="activity-line">{data.activity.slice(0, 8).map(entry => <div key={entry.id}><span className={`event-dot ${entry.status || ""}`}/><b>{entry.action?.replaceAll("_", " ") || entry.source}</b><small>{entry.agent || "north"} · {timeAgo(entry.timestamp)}</small></div>)}</div>
      </Panel>
    </div>
  </div>;
}
