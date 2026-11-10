import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Archive, ArchiveRestore, ChevronDown, ChevronRight, Folder, FolderOpen, Pin, PinOff, Trash2 } from "lucide-react";
import { api, del, patch, post } from "../api";
import { ErrorNotice, InferenceCategories, Markdown, PageHeader, PromptTelemetry, timeAgo } from "../components";
import { Button, confirmDelete, Empty, EntityTable, Loading, Notice, ResourceView, Menu, SearchField, Stack, Status, Tabs, Toolbar, type Column } from "../kit";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import type { Approval, Conversation, Signal, Turn, WorkspaceListing } from "../types";
import { STEP_EVENTS, clock, currentActivity, foldStep, latestThought, spoken, type Step } from "./liveSteps";
import { ApprovalCard } from "./approvalCard";
import { EventList } from "../events";

/** North's self-checks, rendered live rather than only reachable in the ledger.
 *  Each entry turns one SSE payload into a one-line verdict plus its details. */
const SIGNALS: Record<string, (data: any) => Omit<Signal, "event" | "timestamp">> = {
  claims_unverified: d => ({ agent: d.agent, tone: "warn", text: "Claims with no tool evidence", details: d.violations || [] }),
  self_repair_started: d => ({ agent: d.agent, tone: "info", text: "Correcting unsupported claims…", details: [] }),
  self_repair_done: d => ({ agent: d.agent, tone: (d.remaining || []).length ? "warn" : "ok", text: (d.remaining || []).length ? `${d.remaining.length} claim(s) still unsupported` : "Claims corrected", details: d.remaining || [] }),
  critic_flagged: d => ({ agent: d.agent, tone: "warn", text: "Reviewer flagged a gap", details: d.gap ? [d.gap] : [] }),
  handoff_artifact_missing: d => ({ agent: d.agent, tone: "warn", text: `Finished without writing ${d.artifact || "its handoff artifact"}`, details: [] }),
  agent_skipped: d => ({ agent: d.agent, tone: "warn", text: "Skipped - a dependency failed", details: d.failed_dependencies || [] }),
  task_stuck: () => ({ tone: "warn", text: "No progress - cancelled by the watchdog", details: [] }),
  north_star_check_failed: d => ({ tone: "warn", text: "Goal alignment could not be evaluated", details: d.reason ? [d.reason] : [] }),
};

// What stopped dictation, in words. With no onerror, a browser with no microphone, no permission or no
// speech service left the button as it was and said nothing (issue #76, W12).
const DICTATION_PROBLEMS: Record<string, string> = {
  "not-allowed": "This browser is not allowed to use the microphone. Allow it in the site's settings, then try again.",
  "service-not-allowed": "This browser's speech service is not available here.",
  "audio-capture": "No microphone was found.",
  network: "The browser's speech service could not be reached.",
  "no-speech": "No speech was heard.",
};

export const dictationProblem = (error: string) => DICTATION_PROBLEMS[error] || `Dictation stopped: ${error}.`;

/** What a turn is in, for its badge and its working line. A card wins over a running
 *  task, since answering it is what moves things; a paused task stays paused, card or not:
 *  shown as "waiting for approval", the pause was invisible until the API was asked. */
export function turnStatus(taskStatus: string, waiting: boolean, outcome = ""): string {
  if (taskStatus === "paused") return "paused";
  if (waiting) return "waiting_for_approval";
  return taskStatus === "completed" ? outcome || "response_ready" : taskStatus;
}

/** The grey line where the answer will appear: North's newest thought, else what it is doing. */
export function ghostText(paused: boolean, reasoning: string, steps: Step[]): string {
  if (paused) return "Paused.";
  const activity = currentActivity(steps);
  return latestThought(reasoning) || (activity ? `${activity}…` : "Starting…");
}

/** The reply to show. While a task runs, its live text is the newest; once it ends, the
 *  saved answer is the one a reopened page shows, so a page that watched the task live
 *  shows it too, not the stream it pieced together along the way. */
export function replyText(isActive: boolean, streamed = "", output = ""): string {
  return isActive ? streamed || output : output || streamed;
}

function useTaskStreams(turns: Turn[], reload: () => Promise<void>, reloadApprovals: () => Promise<void>) {
  const [live, setLive] = useState<Record<string, string>>({});
  // What North thinks on the way to its answer, streamed apart from the answer itself.
  const [reasoning, setReasoning] = useState<Record<string, string>>({});
  const [signals, setSignals] = useState<Record<string, Signal[]>>({});
  // What the task is doing, step by step, from the same events the TUI renders live.
  const [steps, setSteps] = useState<Record<string, Step[]>>({});
  // One stream per task: a message sent into a running task is a turn of its own on the same task.
  const ids = [...new Set(turns.filter(t => t.task_id && !["completed", "failed", "cancelled", "needs_attention"].includes(t.detail?.task.status || "")).map(t => t.task_id!))];
  useEffect(() => {
    const streams = ids.map(taskId => {
      const stream = new EventSource(`/orchestrator/stream/${taskId}`);
      // Every (re)connect replays the task's whole history, so it starts from nothing: appended to
      // what was shown, a reconnect or a new turn showed the streamed answer twice.
      const restart = () => {
        setLive(current => ({ ...current, [taskId]: "" }));
        setReasoning(current => ({ ...current, [taskId]: "" }));
        setSignals(current => ({ ...current, [taskId]: [] }));
        setSteps(current => ({ ...current, [taskId]: [] }));
      };
      stream.addEventListener("open", restart);
      stream.addEventListener("reasoning", event => {
        const payload = JSON.parse((event as MessageEvent).data);
        setReasoning(current => ({ ...current, [taskId]: (current[taskId] || "") + (payload.text || "") }));
      });
      for (const eventName of STEP_EVENTS) {
        stream.addEventListener(eventName, event => {
          const payload = JSON.parse((event as MessageEvent).data);
          setSteps(current => ({ ...current, [taskId]: foldStep(current[taskId] || [], eventName, payload) }));
        });
      }
      // What streamed before a tool call was narration on the way to it ("I'll check X."), not the
      // answer; kept, it was glued onto the answer that streams after the last tool call.
      stream.addEventListener("tool_called", () => setLive(current => ({ ...current, [taskId]: "" })));
      stream.addEventListener("token", event => {
        const payload = JSON.parse((event as MessageEvent).data);
        setLive(current => ({ ...current, [taskId]: (current[taskId] || "") + (payload.text || "") }));
      });
      for (const [eventName, build] of Object.entries(SIGNALS)) {
        stream.addEventListener(eventName, event => {
          const payload = JSON.parse((event as MessageEvent).data);
          const signal: Signal = { event: eventName, timestamp: payload.timestamp, ...build(payload) };
          setSignals(current => ({ ...current, [taskId]: [...(current[taskId] || []), signal] }));
        });
      }
      // What was streamed is being replaced: a model switched mid-answer, or North answers again with a
      // message you sent while it worked.
      stream.addEventListener("stream_reset", () => setLive(current => ({ ...current, [taskId]: "" })));
      // A retry starts the task over: what the failed try streamed and warned no longer applies.
      stream.addEventListener("task_retrying", restart);
      for (const eventName of ["task_completed", "task_failed", "task_cancelled", "task_skipped", "task_rejected", "task_needs_attention"]) {
        stream.addEventListener(eventName, () => { stream.close(); void reload(); });
      }
      for (const eventName of ["approval_required", "question_required", "approval_responded", "task_paused", "task_resumed"]) {
        stream.addEventListener(eventName, () => { void reload(); void reloadApprovals(); });
      }
      return stream;
    });
    return () => streams.forEach(stream => stream.close());
  }, [ids.join("|"), reload, reloadApprovals]);
  return { live, reasoning, signals, steps };
}

/** A clock that ticks each second while *running*, for the time a turn has taken. */
function useNow(running: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  return now;
}

const STEP_MARKS: Record<string, string> = { "✓": "trace-ok", "✗": "trace-failed", "?": "trace-wait", "▲": "trace-wait" };

/** Every step of the turn, what North thought, and what it cost: the detail the quiet view folds away. */
function Trace({ steps, reasoning, footer, children }: { steps: Step[]; reasoning: string; footer: string[]; children?: React.ReactNode }) {
  return <div className="turn-trace">
    {steps.map((step, index) => <div className="trace-step" key={index}>
      {step.active ? <i className="trace-spin" aria-label="In progress"/> : <span className={STEP_MARKS[step.icon] || "trace-other"}>{step.icon}</span>}
      <span>{step.text}</span>
    </div>)}
    {reasoning.trim() && <p className="trace-thought">{reasoning.trim()}</p>}
    {footer.length > 0 && <footer>{footer.join(" · ")}</footer>}
    {children}
  </div>;
}

function DetailSection({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return <details className="detail-section"><summary><span>{title}</span>{count !== undefined && <em>{count}</em>}<i>⌄</i></summary><div>{children}</div></details>;
}

/** North's self-checks for one turn. Warnings show unprompted; clean results stay
 *  behind a summary, so a run that failed a check never looks like one that passed. */
function SignalStrip({ signals }: { signals: Signal[] }) {
  if (!signals.length) return null;
  const warnings = signals.filter(signal => signal.tone === "warn");
  const rest = signals.filter(signal => signal.tone !== "warn");
  const row = (signal: Signal, index: number) => <div className={`signal-row signal-${signal.tone}`} key={`${signal.event}-${index}`}>
    <span className="signal-mark">{signal.tone === "warn" ? "⚠" : signal.tone === "ok" ? "✓" : "•"}</span>
    <div><b>{signal.text}</b>{signal.agent && <small>{signal.agent}</small>}{signal.details.map((detail, i) => <p key={i}>{detail}</p>)}</div>
  </div>;
  return <div className="signal-strip">
    {warnings.map(row)}
    {rest.length > 0 && <details className="signal-passed"><summary>{rest.length} check{rest.length === 1 ? "" : "s"} passed</summary><div>{rest.map(row)}</div></details>}
  </div>;
}

function WorkspacePicker({ workspace, saving, onSelect }: { workspace: string; saving: boolean; onSelect: (workspace: string) => Promise<boolean> }) {
  const [open, setOpen] = useState(false);
  const [listing, setListing] = useState<WorkspaceListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const picker = useRef<HTMLDivElement>(null);
  const browse = async (path = "") => {
    setOpen(true); setLoading(true); setError("");
    try { setListing(await api<WorkspaceListing>(`/web/api/workspaces${path ? `?path=${encodeURIComponent(path)}` : ""}`)); }
    catch (browseError) { setError(browseError instanceof Error ? browseError.message : String(browseError)); }
    finally { setLoading(false); }
  };
  useEffect(() => { setOpen(false); setListing(null); setError(""); }, [workspace]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!picker.current?.contains(event.target as Node)) setOpen(false); };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open]);
  const choose = async () => { if (listing && await onSelect(listing.path)) setOpen(false); };
  const name = workspace.split("/").filter(Boolean).pop() || "Choose folder";
  return <div className="workspace-select" ref={picker}>
    <button type="button" className="workspace-trigger" disabled={saving} aria-haspopup="dialog" aria-expanded={open} onClick={() => open ? setOpen(false) : void browse(workspace)} title={workspace || "Choose a workspace"}><Folder size={13}/><span><small>Workspace</small><b>{name}</b></span><ChevronDown size={13}/></button>
    {open && <div className="workspace-menu" role="dialog" aria-label="Choose a workspace folder">
      <header><FolderOpen size={14}/><div><small>Choose a folder</small><b title={listing?.path}>{listing?.path || "Directories on this machine"}</b></div></header>
      <div className="workspace-folders">{loading ? <Loading/> : error ? <><ErrorNotice message={error}/><button type="button" onClick={() => void browse("/")}><Folder size={14}/><span>Browse from computer</span><ChevronRight size={13}/></button></> : <>{listing?.parent && <button type="button" onClick={() => void browse(listing.parent)}><Folder size={14}/><span>..</span><small>Parent folder</small><ChevronRight size={13}/></button>}{listing?.directories.map(directory => <button type="button" key={directory.path} onClick={() => void browse(directory.path)}><Folder size={14}/><span>{directory.name}</span><ChevronRight size={13}/></button>)}{listing && !listing.directories.length && <Empty>No folders inside this directory.</Empty>}</>}</div>
      {listing && <footer><button type="button" onClick={() => setOpen(false)}>Cancel</button><button type="button" className="workspace-choose" disabled={saving} onClick={() => void choose()}>{saving ? "Saving…" : "Choose this folder"}</button></footer>}
    </div>}
  </div>;
}

/** A turn whose task an earlier turn started: the message went into that running task (CODING_STYLE §15.4). */
export function joinedTurn(turns: Turn[], index: number): boolean {
  const taskId = turns[index]?.task_id;
  return Boolean(taskId) && turns.slice(0, index).some(turn => turn.task_id === taskId);
}

function JoinedTurn({ turn }: { turn: Turn }) {
  return <article className="turn-bundle turn-joined">
    <div className="user-prompt"><div className="user-message"><div className="turn-meta">You · {timeAgo(turn.created_at)} · sent into the task above</div><p>{turn.prompt}</p></div><div className="avatar user-avatar">You</div></div>
  </article>;
}

export function TurnBundle({ turn, streamed, reasoning = "", signals = [], steps = [], reload, pendingApprovals, respondApproval }: { turn: Turn; streamed?: string; reasoning?: string; signals?: Signal[]; steps?: Step[]; reload: () => Promise<void>; pendingApprovals: Approval[]; respondApproval: (card: Approval, decision: string, option: string, values: Record<string, unknown>, reason?: string) => Promise<void> }) {
  const detail = turn.detail;
  const taskStatus = detail?.task.status || "pending";
  const status = turnStatus(taskStatus, pendingApprovals.length > 0, detail?.outcome_status);
  const entries = detail?.entries || [];
  const runs = detail?.runs || [];
  const inferenceCategories = detail?.inference_categories || [];
  const agents = [...new Set(runs.map(run => run.agent))];
  const tools = entries.filter(entry => entry.action?.includes("tool") || (entry.tools_used?.length || 0) > 0);
  const approvals = entries.filter(entry => entry.source === "approval" || entry.action?.includes("approval"));
  // "unverified" does not contain "verify", so claims_unverified and critic_flagged
  // were both missing from this section even though they are its whole point.
  const verification = entries.filter(entry => /verif|dod|review|repair|critic|handoff|auto_verify/.test(entry.action || ""));
  const cost = inferenceCategories.length
    ? inferenceCategories.reduce((total, item) => total + item.cost_usd, 0)
    : runs.reduce((total, run) => total + (run.cost_usd || 0), 0);
  const responseTimestamp = [...entries].reverse().find(entry => (entry.action === "agent_completed" || entry.action === "task_synthesis") && entry.output)?.timestamp;
  const isActive = ["waiting_for_approval", "paused", "pending", "running", "queued", "retrying", "working"].includes(status);
  const reply = replyText(isActive, streamed, detail?.output);
  const control = async (action: "resume" | "cancel") => {
    if (!turn.task_id) return;
    if (action === "cancel") await api(`/orchestrator/task/${turn.task_id}`, { method: "DELETE" });
    else await api(`/orchestrator/task/${turn.task_id}/${action}`, { method: "POST" });
    await reload();
  };
  const [open, setOpen] = useState(false);
  const now = useNow(isActive);
  const waiting = pendingApprovals.length > 0;
  const paused = taskStatus === "paused";
  // Thinking, shown as grey text where the answer will go, until there is an answer or a card to show.
  const thinking = isActive && !waiting && !reply;
  const lastEntry = entries[entries.length - 1]?.timestamp;
  const endedAt = isActive ? (waiting ? Date.parse(pendingApprovals[0].created_at) || now : now) : Date.parse(lastEntry || "") || now;
  const elapsed = Math.max(0, endedAt - Date.parse(turn.created_at));
  const tokens = runs.reduce((n, r) => n + r.tokens_in + r.tokens_out, 0);
  const models = [...new Set(runs.flatMap(run => run.models_used))];
  const footer = [agents.join(", "), models.join(", "), clock(elapsed), tokens ? `${tokens.toLocaleString()} tokens` : "", cost ? `$${cost.toFixed(4)}` : ""].filter(Boolean);
  const stepCount = steps.length ? `${steps.length} step${steps.length === 1 ? "" : "s"}` : "";
  const stop = ["pending", "running", "queued", "retrying", "paused"].includes(taskStatus) && <Button variant="link" onClick={() => control("cancel")}>Stop</Button>;
  return <article className="turn-bundle">
    <div className="user-prompt"><div className="user-message"><div className="turn-meta">You · {timeAgo(turn.created_at)}</div><p>{turn.prompt}</p></div><div className="avatar user-avatar">You</div></div>
    <div className="north-response"><div className="avatar north-avatar">N</div><div className="response-body">
      <div className="response-heading"><div><b>North</b>{responseTimestamp && <small>{timeAgo(responseTimestamp)}</small>}</div></div>
      {thinking ? <>
        <div className="ghost-thought"><span className={paused ? "" : "shimmer"}>{ghostText(paused, reasoning, steps)}</span></div>
        <div className="turn-quiet">
          {stepCount && <Button variant="link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide steps" : stepCount}</Button>}
          <span>{clock(elapsed)}</span>
          {paused && <Button variant="link" onClick={() => control("resume")}>Resume</Button>}
          {stop}
        </div>
      </> : <div className="turn-quiet">
        <Button variant="link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "⌄" : "›"} Thought for {spoken(elapsed)}{stepCount && ` · ${stepCount}`}</Button>
        {isActive && stop}
      </div>}
      {open && <Trace steps={steps} reasoning={reasoning} footer={footer}>{detail && <details className="trace-more"><summary>Everything North recorded</summary><div className="execution-details">
        <DetailSection title="Plan and routing" count={entries.filter(e => /classif|rout|plan|north_star/.test(e.action || "")).length}>
          <EventList entries={entries.filter(e => /classif|rout|plan|north_star/.test(e.action || ""))}/>
        </DetailSection>
        <DetailSection title="Agent runs" count={runs.length}>
          {runs.length ? runs.map(run => <div className="run-card" key={run.run_id}><div><b>{run.agent}</b><Status value={run.status}/></div><small>Attempt {run.attempt + 1} · {run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : "running"} · {run.providers_used?.join(", ") || "provider pending"} · {run.models_used.join(", ") || "model pending"}</small><PromptTelemetry run={run}/>{run.error && <p className="error-text">{run.error}</p>}</div>) : <Empty>No agent runs recorded yet.</Empty>}
        </DetailSection>
        <DetailSection title="Model calls" count={inferenceCategories.reduce((total, item) => total + item.calls, 0)}>
          <InferenceCategories categories={inferenceCategories}/>
        </DetailSection>
        <DetailSection title="Tools and skills" count={tools.length + runs.flatMap(r => r.skills).length}>
          <EventList entries={tools}/>{runs.flatMap(run => run.skills.map(skill => <div className="skill-chip" key={`${run.run_id}-${skill.name}`}>{skill.name} <small>v{skill.version}</small></div>))}
        </DetailSection>
        <DetailSection title="Approvals and verification" count={approvals.length + verification.length}>
          <EventList entries={[...approvals, ...verification]}/>
        </DetailSection>
        <DetailSection title="Complete timeline" count={entries.length}>
          <EventList entries={entries}/>
        </DetailSection>
      </div></details>}</Trace>}
      {pendingApprovals.length > 0 && <Stack>{pendingApprovals.map(card => <ApprovalCard key={card.id} card={card} onDecide={(decision, option, values, reason) => respondApproval(card, decision, option, values, reason)}/>)}</Stack>}
      {reply && <Markdown>{reply}</Markdown>}
      {!isActive && !reply && <Empty>No response was recorded for this task.</Empty>}
      <SignalStrip signals={signals}/>
    </div></div>
  </article>;
}

/** What deleting a session actually does, said precisely: the wrapper and its
 *  turn list go; the task runs and ledger history behind those turns do not,
 *  and stay reachable from the task list. Shared by the list and room views
 *  so the two delete actions can never describe the operation differently. */
const DELETE_SESSION_COPY =
  "This removes the session and its turn list. It cannot be undone. Task runs and ledger " +
  "history for the work done here are kept and stay reachable from the task list.";

/** What the empty session list says, which depends on *why* it's empty: a
 *  search with no match reads differently from a tab that's genuinely empty,
 *  and "no archived sessions" would be a lie shown on the active tab. */
// An attachment goes into the message as text, so only text can be attached. A
// screenshot read as text became 55 KB of replacement characters in the prompt,
// and North never saw the picture. Text is valid UTF-8 with no NUL character.
export function attachmentText(bytes: ArrayBuffer): string | null {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return text.includes("\u0000") ? null : text;
  } catch {
    return null;
  }
}

export function sessionsEmptyMessage(search: string, tab: "active" | "archived"): string {
  if (search) return "No sessions match this search.";
  return tab === "archived" ? "No archived sessions." : "No sessions have been created yet.";
}

export function Sessions() {
  const navigate = useNavigate();
  const dialog = useDialog();
  const [tab, setTab] = useState<"active" | "archived">("active");
  const sessions = useResource<Conversation[]>(`/web/api/conversations?archived=${tab === "archived"}`, 5000);
  const [search, setSearch] = useState("");
  const visibleSessions = useMemo(() => (sessions.data || []).filter(session => session.title.toLowerCase().includes(search.toLowerCase())), [sessions.data, search]);
  const createSession = async () => {
    const session = await post<Conversation>("/web/api/conversations", { title: "New session" });
    await sessions.reload();
    navigate(`/sessions/${session.id}`);
  };
  const togglePinned = async (session: Conversation) => {
    await patch(`/web/api/conversations/${session.id}`, { pinned: !session.pinned });
    await sessions.reload();
  };
  const toggleArchived = async (session: Conversation) => {
    await patch(`/web/api/conversations/${session.id}`, { archived: !session.archived });
    await sessions.reload();
  };
  const deleteSession = async (session: Conversation) => {
    if (!await confirmDelete(dialog, { noun: "session", name: session.title, consequence: DELETE_SESSION_COPY })) return;
    await del(`/web/api/conversations/${session.id}`);
    await sessions.reload();
  };
  const cell = (session: Conversation, key: string) => {
    if (key === "session") return <><b>{session.pinned && <Pin size={11} aria-label="Pinned"/>} {session.title}</b><small>{session.goal || (session.source === "cli" ? "CLI session" : "North session")}</small></>;
    if (key === "workspace") return session.workspace || "No workspace selected";
    if (key === "activity") return timeAgo(session.updated_at);
    if (key === "turns") return session.turn_count;
    return <Status value={session.archived ? "archived" : session.goal_status}/>;
  };
  const rowActions = (session: Conversation) => <>
    <Button variant="icon" onClick={() => void togglePinned(session)} aria-label={session.pinned ? "Unpin session" : "Pin session"} title={session.pinned ? "Unpin" : "Pin"} icon={session.pinned ? <PinOff size={13}/> : <Pin size={13}/>}/>
    <Button variant="icon" onClick={() => void toggleArchived(session)} aria-label={session.archived ? "Unarchive session" : "Archive session"} title={session.archived ? "Unarchive" : "Archive"} icon={session.archived ? <ArchiveRestore size={13}/> : <Archive size={13}/>}/>
    <Button variant="danger" size="sm" onClick={() => void deleteSession(session)} aria-label="Delete session" title="Delete" icon={<Trash2 size={13}/>}/>
  </>;
  return <div className="page capability-page session-page">
    <PageHeader title="Sessions" subtitle="Open a session to continue work with North." actions={<Button variant="primary" onClick={() => void createSession()}>+ New session</Button>} />
    {sessions.error && sessions.data && <Notice tone="error">{sessions.error}</Notice>}
    <Toolbar
      tabs={<Tabs label="Session filter" value={tab} onChange={setTab} items={[{ value: "active", label: "Active" }, { value: "archived", label: "Archived" }]}/>}
      search={<SearchField label="Search sessions" value={search} onChange={setSearch}
        count={`${visibleSessions.length} session${visibleSessions.length === 1 ? "" : "s"}`}/>}/>
    <ResourceView resource={sessions}>
      {() => <EntityTable label="Sessions" columns={SESSION_COLUMNS} rows={visibleSessions} rowKey={session => session.id}
        to={session => `/sessions/${session.id}`} cell={cell} rowActions={rowActions}
        empty={sessionsEmptyMessage(search, tab)}/>}
    </ResourceView>
  </div>;
}

const SESSION_COLUMNS: Column[] = [
  { key: "session", label: "Session", width: "minmax(0, 2fr)" },
  { key: "workspace", label: "Workspace", width: "minmax(0, 1.4fr)" },
  { key: "activity", label: "Activity", width: "minmax(0, .7fr)" },
  { key: "turns", label: "Turns", width: "minmax(0, .5fr)" },
  { key: "status", label: "Status", width: "minmax(0, .8fr)" },
];

export function Chat() {
  // Namespaced rather than destructured: `prompt` is already the composer's
  // text here, and `confirm`/`alert` would shadow the globals they replace.
  const dialog = useDialog();
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const chats = useResource<Conversation[]>("/web/api/conversations", 5000);
  const room = useResource<Conversation>(conversationId ? `/web/api/conversations/${conversationId}` : null, 5000);
  const approvalResource = useResource<Approval[]>("/web/api/approvals", 2000);
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [recording, setRecording] = useState(false);
  const [notice, setNotice] = useState("");
  // A confirmation fades on its own; an error stays until clicked. "Response received.
  // North is continuing the task." used to stay on screen long after the task ended.
  const flash = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(current => (current === message ? "" : current)), 5000);
  };
  const [savingWorkspace, setSavingWorkspace] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const chatRoom = useRef<HTMLElement>(null);
  const openedAtBottom = useRef<string | null>(null);
  const { live, reasoning, signals, steps } = useTaskStreams(room.data?.turns || [], room.reload, approvalResource.reload);
  useEffect(() => { setPrompt(conversationId ? localStorage.getItem(`north-chat-draft:${conversationId}`) || "" : ""); }, [conversationId]);
  useEffect(() => {
    const element = composerInput.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${element.scrollHeight}px`;
  }, [prompt]);
  useEffect(() => {
    if (!conversationId) { openedAtBottom.current = null; return; }
    if (room.data?.id !== conversationId || openedAtBottom.current === conversationId) return;
    openedAtBottom.current = conversationId;
    window.requestAnimationFrame(() => chatRoom.current?.scrollTo({ top: chatRoom.current.scrollHeight }));
  }, [conversationId, room.data?.id]);
  const updatePrompt = (value: string) => { setPrompt(value); if (conversationId) { if (value) localStorage.setItem(`north-chat-draft:${conversationId}`, value); else localStorage.removeItem(`north-chat-draft:${conversationId}`); } };
  const scrollLatestTurnToTop = () => window.requestAnimationFrame(() => chatRoom.current?.querySelector(".turn-bundle:last-child")?.scrollIntoView({ block: "start", behavior: "smooth" }));
  const createSession = async () => {
    const session = await post<Conversation>("/web/api/conversations", { title: "New session" });
    await chats.reload(); navigate(`/sessions/${session.id}`);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!prompt.trim() || !conversationId) return;
    if (prompt.trim().startsWith("/") && await handleSlash(prompt.trim())) return;
    setSubmitting(true);
    try { await post(`/web/api/conversations/${conversationId}/turns`, { prompt }); updatePrompt(""); setNotice(""); await Promise.all([room.reload(), chats.reload()]); scrollLatestTurnToTop(); }
    catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
    finally { setSubmitting(false); }
  };
  const handleSlash = async (command: string) => {
    const [name] = command.toLowerCase().split(/\s+/, 1);
    if (name === "/new") { updatePrompt(""); await createSession(); return true; }
    if (name === "/flows" || name === "/schedule") { updatePrompt(""); navigate("/flows"); return true; }
    if (name === "/approvals") { updatePrompt(""); navigate("/approvals"); return true; }
    if (name === "/clear") { updatePrompt(""); flash("Draft cleared."); return true; }
    if (name === "/help") { setNotice("Commands: /new, /clear, /status, /pause, /resume, /cancel, /flows, /approvals"); return true; }
    if (name === "/status") { await Promise.all([room.reload(), approvalResource.reload()]); flash("Task status refreshed."); updatePrompt(""); return true; }
    if (["/pause", "/resume", "/cancel"].includes(name)) {
      const turn = [...(room.data?.turns || [])].reverse().find(item => item.task_id && !["completed", "failed", "cancelled"].includes(item.detail?.task.status || ""));
    if (!turn?.task_id) { setNotice("No active task in this session."); return true; }
      const action = name.slice(1);
      if (action === "cancel") await api(`/orchestrator/task/${turn.task_id}`, { method: "DELETE" });
      else await api(`/orchestrator/task/${turn.task_id}/${action}`, { method: "POST" });
      updatePrompt(""); await room.reload(); return true;
    }
    return false;
  };
  const toggleMic = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) { void dialog.alert("Voice dictation is not available in this browser.", { title: "No dictation here" }); return; }
    const recognition = new SpeechRecognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.onstart = () => setRecording(true);
    recognition.onend = () => setRecording(false);
    recognition.onresult = (event: any) => updatePrompt(`${prompt}${prompt ? " " : ""}${event.results[0][0].transcript}`);
    recognition.onerror = (event: any) => {
      setRecording(false);
      if (event.error !== "aborted") void dialog.alert(dictationProblem(event.error), { title: "Dictation stopped" });
    };
    recognition.start();
  };
  const appendFiles = async (files: File[]) => {
    let value = prompt;
    let truncated = false;
    const skipped: string[] = [];
    for (const file of files) {
      const text = attachmentText(await file.arrayBuffer());
      if (text === null) { skipped.push(file.name); continue; }
      const prefix = `${value ? "\n\n" : ""}[Attached: ${file.name}]\n`;
      const remaining = Math.max(0, 30_000 - value.length - prefix.length);
      value += prefix + text.slice(0, remaining);
      if (text.length > remaining) truncated = true;
      if (value.length >= 30_000) break;
    }
    updatePrompt(value);
    const notices = [
      skipped.length ? `Not attached: ${skipped.join(", ")}. Only text files can be attached; North cannot read images or other binary files yet.` : "",
      truncated ? "Attachment text was shortened to fit the session message limit." : "",
    ].filter(Boolean);
    if (notices.length) setNotice(notices.join(" "));
  };
  const attachFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    await appendFiles(Array.from(event.target.files || []));
    event.target.value = "";
  };
  const respondApproval = async (card: Approval, decision: string, option = "", values: Record<string, unknown> = {}, reason = "") => { try { await post("/orchestrator/approval/respond", { card_id: card.id, decision, chosen_option: option, values, reason }); flash("Response received. North is continuing the task."); await Promise.all([approvalResource.reload(), room.reload()]); } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); } };
  const rename = async () => {
    if (!room.data) return;
    const title = await dialog.prompt("What should this session be called?", room.data.title,
      { title: "Rename session", confirmLabel: "Rename" });
    if (title) { await patch(`/web/api/conversations/${room.data.id}`, { title }); await Promise.all([room.reload(), chats.reload()]); }
  };
  const editGoal = async () => {
    if (!room.data) return;
    const goal = await dialog.prompt("What outcome should North keep working toward?", room.data.goal,
      { title: "Edit active goal", confirmLabel: "Save goal" });
    if (goal !== null) {
      await patch(`/web/api/conversations/${room.data.id}`, { goal, goal_status: goal.trim() ? "active" : "idle" });
      await Promise.all([room.reload(), chats.reload()]);
    }
  };
  const markGoalAchieved = async () => {
    if (!room.data) return;
    await patch(`/web/api/conversations/${room.data.id}`, { goal_status: "achieved" });
    await Promise.all([room.reload(), chats.reload()]);
  };
  const compactGoal = room.data?.goal
    ? `${room.data.goal.slice(0, 110)}${room.data.goal.length > 110 ? "…" : ""}`
    : "No active goal";
  const changeWorkspace = async (workspace: string) => {
    if (!room.data || workspace === room.data.workspace) return true;
    setSavingWorkspace(true);
    try {
      await patch(`/web/api/conversations/${room.data.id}`, { workspace });
      flash("Workspace updated for this session.");
      await Promise.all([room.reload(), chats.reload()]);
      return true;
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); return false; }
    finally { setSavingWorkspace(false); }
  };
  const deleteChat = async (session: Conversation) => {
    if (!await confirmDelete(dialog, { noun: "session", name: session.title, consequence: DELETE_SESSION_COPY })) return;
    await del(`/web/api/conversations/${session.id}`);
    await chats.reload();
    if (session.id === conversationId) navigate("/sessions");
  };
  const togglePinned = async () => {
    if (!room.data) return;
    await patch(`/web/api/conversations/${room.data.id}`, { pinned: !room.data.pinned });
    await Promise.all([room.reload(), chats.reload()]);
  };
  const toggleArchived = async () => {
    if (!room.data) return;
    await patch(`/web/api/conversations/${room.data.id}`, { archived: !room.data.archived });
    await Promise.all([room.reload(), chats.reload()]);
  };
  const currentWorkspace = room.data?.workspace || "";
  const taskRunning = (room.data?.turns || []).some(turn => turn.task_id && ["pending", "running", "queued", "retrying", "paused"].includes(turn.detail?.task.status || ""));
  return <div className={`chat-page session-room ${dragging ? "dragging" : ""}`} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void appendFiles(Array.from(event.dataTransfer.files)); }}>
    <section className="chat-room" ref={chatRoom}>
      {!conversationId ? <div className="chat-welcome"><div className="welcome-wordmark" aria-label="North">north<span aria-hidden="true">.</span></div><h1>What are we working on?</h1><p>Start a new session or return to one of your previous sessions.</p><Button variant="primary" onClick={() => void createSession()}>+ New session</Button></div> : room.loading && !room.data ? <Loading/> : !room.data ? <Notice tone="error">{room.error || "Session unavailable"}</Notice> : <>
        <PageHeader title={room.data.title} subtitle={`${room.data.turns?.length || 0} prompts · ${compactGoal}`} actions={<Menu items={[
          { label: room.data.pinned ? "Unpin" : "Pin", onSelect: () => void togglePinned() },
          { label: "Rename", onSelect: () => void rename() },
          { label: "Edit goal", onSelect: () => void editGoal() },
          { label: room.data.archived ? "Unarchive" : "Archive", onSelect: () => void toggleArchived() },
          { label: "Delete", danger: true, onSelect: () => void deleteChat(room.data!) },
        ]}/>}/>
        {room.error && <Notice tone="error">{room.error}</Notice>}
        <section className="session-goal"><div><small>Active goal</small><p>{room.data.goal || "No goal has been set for this session yet."}</p></div><div><Status value={room.data.goal_status}/>{room.data.goal && room.data.goal_status !== "achieved" && <Button size="sm" onClick={() => void markGoalAchieved()}>Mark achieved</Button>}</div></section>
        <div className="turns">{room.data.turns?.length ? room.data.turns.map((turn, index) => joinedTurn(room.data!.turns!, index) ? <JoinedTurn key={turn.id} turn={turn}/> : <TurnBundle key={turn.id} turn={turn} streamed={turn.task_id ? live[turn.task_id] : ""} reasoning={turn.task_id ? reasoning[turn.task_id] : ""} signals={turn.task_id ? signals[turn.task_id] : []} steps={turn.task_id ? steps[turn.task_id] : []} reload={room.reload} pendingApprovals={(approvalResource.data || []).filter(card => card.task_id === turn.task_id && card.status === "pending")} respondApproval={respondApproval}/>) : <Empty>A fresh room. Your prompts, North's responses and every step of the work will stay together here.</Empty>}</div>
        {notice && <div className="chat-notice" onClick={() => setNotice("")}>{notice}</div>}
        <form className="composer" onSubmit={submit}><textarea ref={composerInput} value={prompt} onChange={e => updatePrompt(e.target.value)} placeholder={taskRunning ? "Add to the task North is working on…" : "Ask North anything…"} rows={1} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><div className="composer-footer"><WorkspacePicker workspace={currentWorkspace} saving={savingWorkspace} onSelect={changeWorkspace}/><div className="composer-actions"><input ref={fileInput} type="file" multiple hidden onChange={attachFile}/><button type="button" className="composer-tool" onClick={() => fileInput.current?.click()} aria-label="Attach files" title="Attach files or drag them here">＋</button><button type="button" className={`composer-tool ${recording ? "recording" : ""}`} onClick={toggleMic} aria-label="Use microphone" title="Use microphone">{recording ? "■" : "♩"}</button><button disabled={submitting || !prompt.trim()}>{submitting ? "Starting…" : "Send ↑"}</button></div></div></form>
      </>}
    </section>
  </div>;
}
