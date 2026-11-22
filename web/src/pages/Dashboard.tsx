import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { formatDateTime, PageHeader, Panel, timeAgo } from "../components";
import { Button, Empty, ListRow, Loading, Notice, Status, type Tone } from "../kit";
import { useHealth, useResource } from "../hooks";
import { useInbox, waitedLong, waitingFor } from "../inbox";
import type { Artifact, Conversation, DashboardData } from "../types";
import { SetupReminder } from "./Setup";

/** The approval mode. Its home is Settings; the dashboard no longer shows it. Kept for older callers. */
export function dialsLine(system: DashboardData["system"]): string {
  return `Autonomy ${system.autonomy}`;
}

/** The System facts, one row each. Home shows only the ones that are a problem (see `problems`). */
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

interface CodingAgent { name: string; ready: boolean; detail: string }
interface RunLite { run_id: string; flow: string; status: string; started_at: string }
interface Problem { key: string; title: string; detail: string; to: string; tone: Tone; state: string }

const LAST_SEEN = "north.home.lastSeen";
const SINCE = "north.home.since";
const DAY = 86_400_000;
const AWAY = 30 * 60_000;

/**
 * Since when "What changed?" counts: the start of your last time away. Looking at Home keeps
 * the same point while you stay; after 30 minutes away, a new visit starts from when you left.
 * A first visit looks back one day.
 */
export function visitSince(now: number, lastSeen: number, since: number): { since: number; lastSeen: number } {
  if (!lastSeen) return { since: now - DAY, lastSeen: now };
  if (now - lastSeen > AWAY) return { since: lastSeen, lastSeen: now };
  return { since: since || now - DAY, lastSeen: now };
}

function useSinceLastVisit(): number {
  const [since] = useState(() => {
    try {
      const next = visitSince(Date.now(), Number(localStorage.getItem(LAST_SEEN)), Number(localStorage.getItem(SINCE)));
      localStorage.setItem(SINCE, String(next.since));
      localStorage.setItem(LAST_SEEN, String(next.lastSeen));
      return next.since;
    } catch { return Date.now() - DAY; }
  });
  useEffect(() => {
    const seen = () => { try { localStorage.setItem(LAST_SEEN, String(Date.now())); } catch { /* private window */ } };
    const timer = window.setInterval(seen, 60_000);
    return () => { window.clearInterval(timer); seen(); };
  }, []);
  return since;
}

/** Only what is wrong: a check that failed, a model that cannot answer, an agent that cannot run, a flow whose last run did not finish. */
export function problems(system: DashboardData["system"] | undefined, health: string, agents: CodingAgent[], runs: RunLite[]): Problem[] {
  const list: Problem[] = [];
  if (health === "degraded" || health === "offline") list.push({ key: "health", title: "North needs attention", detail: "A core check failed", to: "/system", tone: "bad", state: health });
  for (const model of system?.models ?? []) if (model.state === "failing") list.push({ key: `m:${model.name}`, title: model.name, detail: model.detail || "cannot answer", to: "/settings", tone: "bad", state: "failing" });
  for (const wait of system?.waiting ?? []) list.push({ key: `w:${wait.model}`, title: wait.model, detail: `${wait.reason || "waiting"} · until ${formatDateTime(wait.until)}`, to: "/settings", tone: "warn", state: "waiting" });
  for (const agent of agents) if (!agent.ready) list.push({ key: `a:${agent.name}`, title: `Coding agent ${agent.name}`, detail: agent.detail || "not ready", to: "/settings", tone: "bad", state: "not ready" });
  const last = new Map<string, RunLite>();
  for (const run of runs) if (!last.has(run.flow)) last.set(run.flow, run);
  for (const run of last.values()) if (run.status === "cancelled" || run.status === "failed")
    list.push({ key: `f:${run.flow}`, title: `Flow ${run.flow}`, detail: `last run ${run.status} ${timeAgo(run.started_at)}`, to: `/flows?open=${encodeURIComponent(run.flow)}`, tone: "bad", state: run.status });
  return list;
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

export function Dashboard() {
  const { data, error, loading, reload } = useResource<DashboardData>("/web/api/dashboard", 10000);
  const inbox = useInbox(10000);
  const health = useHealth();
  const agents = useResource<CodingAgent[]>("/web/api/setup/coding-agents", 60000);
  const files = useResource<Artifact[]>("/web/api/artifacts", 30000);
  const sessions = useResource<Conversation[]>("/web/api/conversations", 30000);
  const since = useSinceLastVisit();
  const header = <PageHeader title="Home" subtitle="What needs you, what changed, and what is broken."
    actions={<Button onClick={() => { void reload(); void inbox.reload(); }}>Refresh</Button>} />;
  if (!data) return <div className="page dashboard-page">
    {header}
    {loading ? <Loading/> : <Notice tone="error">{error || "Home unavailable"}</Notice>}
  </div>;

  const runs = (inbox.runs.data || []) as RunLite[];
  const newRuns = runs.filter(run => new Date(run.started_at).getTime() >= since);
  const byState = (state: string) => newRuns.filter(run => run.status === state).length;
  const stoppedRuns = newRuns.filter(run => run.status === "cancelled" || run.status === "failed").length;
  const newFiles = (files.data || []).filter(file => (file.updated_at || 0) * 1000 >= since);
  const activeSessions = (sessions.data || []).filter(session => new Date(session.updated_at).getTime() >= since);
  const recent = (sessions.data || []).slice(0, 4);
  const broken = problems(data.system, health.state, agents.data || [], runs);
  const sinceText = Date.now() - since < 60_000 ? "just now" : timeAgo(new Date(since).toISOString());

  const needs = [
    ...inbox.stopped.map(run => ({ id: run.run_id, title: `${run.flow} paused`, detail: "flow · Resume or Discard", since: run.updated_at || run.started_at })),
    ...inbox.cards.map(card => ({ id: card.id, title: card.title, detail: `${card.agent} · ${card.blocking ? "something waits on this" : "when you have a minute"}`, since: card.created_at })),
  ].sort((a, b) => new Date(a.since).getTime() - new Date(b.since).getTime());

  return <div className="page dashboard-page home-page">
    {header}
    {error && <Notice tone="error">{error}</Notice>}
    <div className="cockpit-grid">
      <SetupReminder/>

      <Panel title="What needs me?" label={needs.length ? `${needs.length} waiting` : "all clear"} to="/inbox" className={`full-panel ${needs.length ? "attention-panel" : "quiet-panel"}`}>
        {needs.length
          ? needs.slice(0, 5).map(item => <ListRow key={item.id} to={`/inbox?focus=${item.id}`} title={item.title} meta={[item.detail]}
              status={<span className={`wait-chip ${waitedLong(item.since) ? "wait-long" : ""}`}>{waitingFor(item.since)}</span>}/>)
          : <Empty>Nothing needs you.{inbox.info.length > 0 && <> <Link to="/inbox?tab=info">{plural(inbox.info.length, "note")} for your info →</Link></>}</Empty>}
        {needs.length > 5 && <Link className="panel-more" to="/inbox">All {needs.length} in the Inbox →</Link>}
      </Panel>

      <Panel title="What changed?" label={`since ${sinceText}`} className="full-panel">
        <ListRow to="/flows" title="Flows"
          meta={[newRuns.length ? `${plural(newRuns.length, "run")} · ${byState("completed")} completed${stoppedRuns ? ` · ${stoppedRuns} did not finish` : ""}` : "no runs"]}
          status={stoppedRuns ? <Status value="stopped" tone="bad"/> : undefined}/>
        <ListRow to="/artifacts" title="Files" meta={[newFiles.length ? `${plural(newFiles.length, "new file")}: ${newFiles.slice(0, 3).map(file => file.name).join(", ")}` : "no new files"]}/>
        <ListRow to="/inbox?tab=info" title="For your info" meta={[inbox.info.length ? `${plural(inbox.info.length, "note")} from north` : "nothing new"]}/>
        <ListRow to="/sessions" title="Sessions" meta={[activeSessions.length ? `${plural(activeSessions.length, "session")} active` : "no new activity"]}>
          {recent.length > 0 && <div className="chip-row">{recent.map(session =>
            <Link key={session.id} className="session-chip" to={`/sessions/${session.id}`} title={session.title}>{session.title || "Untitled"}</Link>)}</div>}
        </ListRow>
      </Panel>

      <Panel title="Is anything broken?" label={broken.length ? plural(broken.length, "problem") : "no"} className={`full-panel ${broken.length ? "" : "quiet-panel"}`}>
        {broken.length
          ? broken.map(problem => <ListRow key={problem.key} to={problem.to} title={problem.title} meta={[problem.detail]} status={<Status value={problem.state} tone={problem.tone}/>}/>)
          : <Empty>Nothing is broken. Health, models and coding agents are on the System and Settings pages.</Empty>}
      </Panel>
    </div>
  </div>;
}
