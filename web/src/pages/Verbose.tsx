import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api, del, patch, post } from "../api";
import { Empty, ErrorNotice, HealthIndicator, Loading, Markdown, PageHeader, Panel, Status, timeAgo } from "../components";
import { useResource } from "../hooks";
import type { Approval, Artifact, CardField, LedgerEntry, RoutingDecision, RoutingSkip } from "../types";

// One provider's part in a routing walk: what north called and what came back,
// kept apart from what it never called. A walk touches hundreds of endpoints,
// so the rows are only readable once they are folded up this way.
interface ProviderRoll {
  provider: string;
  tried: { label: string; count: number; detail: string }[];
  skipped: { label: string; count: number }[];
  total: number;
}

function tally(skips: RoutingSkip[], label: (skip: RoutingSkip) => string) {
  const rows = new Map<string, { label: string; count: number; detail: string }>();
  for (const skip of skips) {
    const key = label(skip);
    const row = rows.get(key) || { label: key, count: 0, detail: skip.detail || "" };
    rows.set(key, { ...row, count: row.count + 1, detail: row.detail || skip.detail || "" });
  }
  return [...rows.values()].sort((a, b) => b.count - a.count);
}

function rollUp(skips: RoutingSkip[]): ProviderRoll[] {
  const byProvider = new Map<string, RoutingSkip[]>();
  for (const skip of skips) byProvider.set(skip.provider, [...(byProvider.get(skip.provider) || []), skip]);
  return [...byProvider].map(([provider, rows]) => ({
    provider,
    tried: tally(rows.filter(r => r.tried), r => `${r.status_code || "—"} · ${r.reason}`),
    skipped: tally(rows.filter(r => !r.tried), r => r.reason).map(({ label, count }) => ({ label, count })),
    total: rows.length,
  })).sort((a, b) => b.total - a.total);
}

function DecisionCard({ decision }: { decision: RoutingDecision }) {
  const rolls = useMemo(() => rollUp(decision.skipped || []), [decision.skipped]);
  // The stored skip list is capped, so the honest total comes from the row's own
  // counters rather than from counting what happens to have been kept.
  const shown = (decision.skipped || []).length;
  const hidden = Math.max(0, decision.endpoints - shown);
  return <div className="routing-card">
    <header>
      <b>{decision.part}</b>
      {decision.chosen_model
        ? <span>answered by {decision.chosen_model} · {decision.chosen_provider}</span>
        : <span className="routing-exhausted">no model answered</span>}
      <small>{decision.considered} models · {decision.endpoints} endpoints · {decision.attempted} called</small>
    </header>
    {rolls.map(roll => <div className="routing-provider" key={roll.provider}>
      <b>{roll.provider}</b>
      <div>
        {roll.tried.map(row => <div className="routing-line" key={`t${row.label}`}>
          <span className="routing-count">{row.count} called</span>
          <span title={row.label}>{row.label}</span>
          {row.detail && <em title={row.detail}>{row.detail}</em>}
        </div>)}
        {roll.skipped.map(row => <div className="routing-line routing-untried" key={`s${row.label}`}>
          <span className="routing-count">{row.count} skipped</span>
          <span title={row.label}>{row.label}</span>
        </div>)}
      </div>
    </div>)}
    {hidden > 0 && <small className="routing-more">…and {hidden} more endpoints not listed</small>}
  </div>;
}

function RoutingAttempts({ taskId }: { taskId: string }) {
  const resource = useResource<RoutingDecision[]>(`/web/api/routing/decisions?task_id=${encodeURIComponent(taskId)}`);
  if (resource.loading) return <Loading/>;
  if (resource.error) return <ErrorNotice message={resource.error}/>;
  const decisions = resource.data || [];
  if (!decisions.length) return <Empty>No routing record for this task.</Empty>;
  return <div className="routing-detail">{decisions.map(d => <DecisionCard decision={d} key={d.id}/>)}</div>;
}

export function Tasks() {
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=500", 7000);
  const [opened, setOpened] = useState("");
  if (resource.loading) return <Loading/>;
  const tasks = new Map<string, LedgerEntry[]>();
  for (const entry of resource.data || []) if (entry.task_id) tasks.set(entry.task_id, [...(tasks.get(entry.task_id) || []), entry]);
  return <div className="page"><PageHeader eyebrow="Work" title="Tasks" subtitle="Every task, from prompt to final outcome."/>{resource.error && <ErrorNotice message={resource.error}/>}<div className="table-list">
    {[...tasks].map(([id, entries]) => { const latest = entries[0]; const terminal = entries.find(e => e.action?.startsWith("task_completed") || ["task_failed", "task_cancelled"].includes(e.action || "")); const prompt = [...entries].reverse().find(e => e.action === "task_received")?.input; return <div key={id}>
      <div className="table-row task-row" onClick={() => setOpened(opened === id ? "" : id)}><div className="row-main"><b>{prompt || id}</b><small>{id} · {timeAgo(latest.timestamp)} · {opened === id ? "hide" : "show"} models tried</small></div><span>{[...new Set(entries.map(e => e.agent).filter(Boolean))].join(", ") || "orchestrator"}</span><Status value={terminal?.status || "running"}/></div>
      {opened === id && <RoutingAttempts taskId={id}/>}
    </div>; })}
  </div>{!tasks.size && <Empty>No task history yet.</Empty>}</div>;
}

// The pipeline stages, in the order they run - so a task's artifacts read as the
// story of that run rather than in whatever order the filesystem returned them.
const stageOrder = ["research", "architecture", "implementation", "qa"];
const stageIcon: Record<string, string> = { news: "☼", notes: "✎", wellness: "♥", research: "◇", architecture: "▣", implementation: "▸", qa: "✓" };

function ArtifactLibrary({ newsOnly = false }: { newsOnly?: boolean }) {
  const resource = useResource<Artifact[]>("/web/api/artifacts", 10000);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const files = (resource.data || []).filter(file => !newsOnly || file.kind === "news");
  const open = async (file: Artifact) => { try { setSelected(await api<Artifact>(`/web/api/artifacts/${file.id}`)); } catch (err) { setError(String(err)); } };
  const personal = files.filter(file => !file.task);
  // One group per task run, newest run first, stages in pipeline order within it.
  const runs = useMemo(() => {
    const grouped = new Map<string, Artifact[]>();
    for (const file of files) if (file.task) grouped.set(file.task, [...(grouped.get(file.task) || []), file]);
    return [...grouped].map(([task, items]) => ({
      task,
      items: [...items].sort((a, b) => stageOrder.indexOf(a.kind) - stageOrder.indexOf(b.kind) || a.name.localeCompare(b.name)),
      updated: Math.max(...items.map(item => item.updated_at || 0)),
    })).sort((a, b) => b.updated - a.updated);
  }, [files]);
  const card = (file: Artifact) => <button className="artifact-card" key={file.id} onClick={() => open(file)}><span>{stageIcon[file.kind] || "◇"}</span><div><b>{file.name}</b><small>{file.kind} · {file.size ? `${Math.ceil(file.size / 1024)} KB` : ""} · {timeAgo(file.updated_at)}</small></div></button>;
  if (resource.loading) return <Loading/>;
  return <>{(resource.error || error) && <ErrorNotice message={resource.error || error}/>}
    {!!personal.length && <div className="artifact-grid">{personal.map(card)}</div>}
    {runs.map(run => <section className="artifact-run" key={run.task}><h3>{run.task}<small>{timeAgo(run.updated)}</small></h3><div className="artifact-grid">{run.items.map(card)}</div></section>)}
    {!files.length && <Empty>No files have been generated in this section.</Empty>}
    {selected && <div className="document-view"><header><div><span>{selected.task ? `${selected.task} · ${selected.kind}` : selected.kind}</span><h2>{selected.name}</h2></div><button onClick={() => setSelected(null)}>Close</button></header><Markdown>{selected.content || ""}</Markdown></div>}</>;
}

export function Artifacts() { return <div className="page"><PageHeader eyebrow="Outputs" title="Artifacts" subtitle="Every report, briefing, note, plan, and file North has produced."/><ArtifactLibrary/></div>; }

const fieldLabel = (field: CardField) => field.label || (field.name.replace(/_/g, " ").replace(/^./, c => c.toUpperCase()));

// One filled-in field. Read-only ones render as text so the card reads as work
// to check rather than a form to fill: the point is to see what North put there,
// and only the parts it offered as editable invite typing.
function CardFieldRow({ field, value, onChange }: { field: CardField; value: unknown; onChange: (v: unknown) => void }) {
  const text = value === null || value === undefined ? "" : String(value);
  let control;
  if (!field.editable) {
    control = field.type === "link"
      ? <a className="card-field-value" href={text} target="_blank" rel="noreferrer">{text}</a>
      : <div className="card-field-value">{field.type === "boolean" ? (value ? "yes" : "no") : text || "—"}</div>;
  } else if (field.type === "textarea") {
    control = <textarea value={text} rows={6} onChange={e => onChange(e.target.value)}/>;
  } else if (field.type === "boolean") {
    control = <input type="checkbox" checked={Boolean(value)} onChange={e => onChange(e.target.checked)}/>;
  } else if (field.type === "select") {
    control = <select value={text} onChange={e => onChange(e.target.value)}>{field.options.map(o => <option key={o} value={o}>{o}</option>)}</select>;
  } else {
    control = <input type={field.type === "number" ? "number" : "text"} value={text} onChange={e => onChange(e.target.value)}/>;
  }
  return <label className="card-field"><span>{fieldLabel(field)}{field.editable && <em> editable</em>}</span>{control}</label>;
}

// Reviewing does not need to be fast. This page is where something goes out in
// your name, so optimising it for throughput optimises for the failure it exists
// to prevent - a queue cleared in forty seconds and a queue rubber-stamped are
// indistinguishable afterwards. Everything here is in aid of judging one item
// well: the source material beside the work, what the decision will cause said
// out loud, and no way to decide more than one thing at a time.
// Chips for the reasons that come up over and over, plus free text for the one
// that does not. A rejection without a reason says only "no", which cannot be
// learned from - and this is the highest-quality signal north ever gets.
const REJECTION_REASONS = ["Not relevant", "Wrong details", "Already handled", "Bad timing", "Not interested"];

function ApprovalCard({ card, onDecide }: { card: Approval; onDecide: (decision: string, chosen_option: string, values: Record<string, unknown>, reason?: string) => void }) {
  const fields = card.fields || [];
  const [values, setValues] = useState<Record<string, unknown>>(() => Object.fromEntries(fields.map(f => [f.name, f.value])));
  const [showContext, setShowContext] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const edited = fields.filter(f => f.editable && values[f.name] !== f.value).length;
  // Prepared work is judged against its source, so the source is shown. For a
  // guard-rail the context is incidental and stays behind a toggle - collapsing
  // it there is right, and collapsing it here would make deciding-without-
  // looking the default path, which is the specific thing to make harder.
  const sideBySide = !card.blocking && Boolean(card.context);
  const body = <>
    {card.message && <p>{card.message}</p>}
    {fields.length > 0 && <div className="card-fields">{fields.map(field => <CardFieldRow key={field.name} field={field} value={values[field.name]} onChange={v => setValues(prev => ({ ...prev, [field.name]: v }))}/>)}</div>}
    {card.context && !sideBySide && <div className="card-context"><button className="link-button" onClick={() => setShowContext(!showContext)}>{showContext ? "Hide" : "Show"} source</button>{showContext && <pre>{card.context}</pre>}</div>}
  </>;
  return <article className="approval-card">
    <div className="approval-type">{card.type}{!card.blocking && <span className="card-unblocking"> · nothing is waiting on this</span>}</div>
    <h2>{card.title}</h2>
    {sideBySide
      ? <div className="approval-split"><div className="approval-work">{body}</div><aside className="approval-source"><div className="editor-label">Source</div><pre>{card.context}</pre></aside></div>
      : body}
    <small>{card.agent} · {timeAgo(card.created_at)}{edited > 0 && ` · ${edited} field${edited > 1 ? "s" : ""} edited`}</small>
    {/* What approving will actually do. Two cards with identical buttons can
        submit an application and save a draft respectively. */}
    {card.next_step && <p className="approval-consequence">Approving will <b>{card.next_step}</b>.</p>}
    {/* Asked only for prepared work. A guard-rail rejection is a decision about
        one action in one task, not an example of what should be proposed, so
        interrupting it for a reason would cost a click and teach nothing. */}
    {rejecting
      ? <div className="reject-reason">
          <div className="editor-label">Why? This is what makes the next batch better.</div>
          <div className="reason-chips">
            {REJECTION_REASONS.map(chip =>
              <button key={chip} className={reason === chip ? "active" : ""} onClick={() => setReason(chip)}>{chip}</button>)}
          </div>
          <input value={reason} onChange={e => setReason(e.target.value)} placeholder="or say why in your own words…"/>
          <div className="approval-actions">
            <button className="danger-button" onClick={() => onDecide("rejected", "", values, reason)}>Reject</button>
            <button onClick={() => { setRejecting(false); setReason(""); }}>Cancel</button>
          </div>
        </div>
      : <div className="approval-actions">
          {card.type === "question"
            ? card.options.map(option => <button key={option} onClick={() => onDecide("answered", option, values)}>{option}</button>)
            : <><button className="primary-button" onClick={() => onDecide("approved", "", values)}>Approve</button>
                <button className="danger-button" onClick={() => (card.source ? setRejecting(true) : onDecide("rejected", "", values))}>Reject</button></>}
        </div>}
  </article>;
}

export function Approvals() {
  // A slow poll as the floor, with SSE on top. The stream is what makes a new
  // card appear; the poll is what stops a dropped connection turning into a
  // queue that has silently stopped updating.
  const resource = useResource<Approval[]>("/web/api/approvals", 30000);
  const { reload } = resource;
  useEffect(() => {
    const stream = new EventSource("/orchestrator/stream");
    for (const event of ["approval_required", "question_required", "approval_responded"]) {
      stream.addEventListener(event, () => { void reload(); });
    }
    return () => stream.close();
  }, [reload]);

  const decide = async (card: Approval, decision: string, chosen_option = "", values: Record<string, unknown> = {}, reason = "") => { await post("/orchestrator/approval/respond", { card_id: card.id, decision, chosen_option, values, reason }); await resource.reload(); };
  if (resource.loading) return <Loading/>;
  const pending = (resource.data || []).filter(card => card.status === "pending");
  const history = (resource.data || []).filter(card => card.status !== "pending");

  // Split on whether anything is waiting, not on urgency-as-styling. An agent
  // frozen mid-action and an application that can wait until tonight have
  // different costs of being missed, and one undifferentiated stack hides that.
  // Two pages was considered and rejected: two inboxes means missed items.
  const blocking = pending.filter(card => card.blocking);
  const prepared = pending.filter(card => !card.blocking);

  // Deliberately no "approve all". Reject-all would be fine; a bulk approve is
  // the fastest possible review and the worst one, and this page exists for
  // per-item judgement.
  const section = (cards: Approval[]) =>
    <div className="approval-stack">{cards.map(card => <ApprovalCard key={card.id} card={card} onDecide={(d, o, v, r) => decide(card, d, o, v, r)}/>)}</div>;

  return <div className="page">
    <PageHeader eyebrow="Attention" title="Approvals"
      subtitle={pending.length ? `${pending.length} waiting · ${blocking.length} blocking something` : "Questions and consequential actions waiting for your decision."}/>
    {resource.error && <ErrorNotice message={resource.error}/>}

    {blocking.length > 0 && <>
      <h2 className="section-title">Needs you now</h2>
      <p className="muted memory-note">Something is stopped until you answer.</p>
      {section(blocking)}
    </>}

    {prepared.length > 0 && <>
      <h2 className="section-title">When you have a minute</h2>
      <p className="muted memory-note">Work north finished and left for you. Nothing is blocked; take the time to read it.</p>
      {section(prepared)}
    </>}

    {!pending.length && <Empty>Nothing needs your attention.</Empty>}

    <h2 className="section-title">Resolved</h2>
    <div className="table-list">{history.map(card => <div className="table-row" key={card.id}><div className="row-main"><b>{card.title}</b><small>{card.agent} · {timeAgo(card.created_at)}</small></div><Status value={card.status}/></div>)}</div>
  </div>;
}

interface Job {
  job_id: string; agent: string; task: string; status: string;
  scheduled_at: string; scheduled_epoch: number; scheduled_local: string;
  cron_entry?: string | null; label?: string;
}

interface Cron {
  name: string; label: string; title: string; description: string;
  agent: string; task: string; hour: number; minute: number;
  weekdays: number[]; cadence: string; enabled: boolean; tz: string;
  schedule: string; next_run_local: string; next_run_epoch: number; source: string; modified: boolean;
}

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// Statuses a job cannot come back from, and so the only ones that are history.
const FINISHED = new Set(["completed", "failed", "cancelled"]);
const hhmm = (hour: number, minute: number) => `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;

// One way of saying when, used by everything on this page. The page previously
// mixed the server's "2026-09-08 03:00 PDT" with a browser toLocaleString
// ("9/7/2026, 8:00:00 AM") in adjacent panels, which read as two different kinds
// of fact rather than one fact twice.
function whenFromNow(epoch: number): string {
  const seconds = epoch - Date.now() / 1000;
  if (seconds < 0) return "now";
  if (seconds < 90) return "in under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = seconds / 3600;
  if (hours < 24) return `in ${Math.round(hours)} h`;
  const days = Math.round(hours / 24);
  return days === 1 ? "tomorrow" : `in ${days} days`;
}

// The form's own idea of a schedule, before it becomes a request. Days live here
// as a set of numbers because that is what the day buttons toggle; the API is
// given "daily" when none are picked, which is what an empty selection means.
// How often, as a person chooses it - one named rule rather than a set of day
// toggles they have to translate. "Every weekday" was expressible only by
// picking five buttons and knowing that meant weekdays.
type Repeat = "once" | "daily" | "weekdays" | "weekends" | "custom";

const REPEAT_LABELS: [Repeat, string][] = [
  ["once", "Does not repeat"],
  ["daily", "Every day"],
  ["weekdays", "Every weekday (Mon to Fri)"],
  ["weekends", "Every weekend (Sat and Sun)"],
  ["custom", "Weekly on selected days…"],
];

interface Draft {
  label: string; task: string; agent: string;
  hour: number; minute: number; repeat: Repeat; days: number[]; date: string;
}

const todayISO = () => new Date(new Date().getTime() - new Date().getTimezoneOffset() * 60000)
  .toISOString().slice(0, 10);

const emptyDraft = (): Draft => ({
  label: "", task: "", agent: "general", hour: 9, minute: 0,
  repeat: "once", days: [], date: todayISO(),
});

// Which named rule an existing routine is already following, so opening the
// editor shows the rule rather than making the reader infer it from checkboxes.
function repeatOf(weekdays: number[]): Repeat {
  const set = [...weekdays].sort().join(",");
  if (!set) return "daily";
  if (set === "0,1,2,3,4") return "weekdays";
  if (set === "5,6") return "weekends";
  return "custom";
}

const draftOf = (entry: Cron): Draft => ({
  label: entry.label, task: entry.task, agent: entry.agent,
  hour: entry.hour, minute: entry.minute,
  repeat: repeatOf(entry.weekdays), days: [...entry.weekdays], date: todayISO(),
});

// The rule as the API takes it. Only "custom" needs the day list.
const daysField = (draft: Draft) =>
  draft.repeat === "custom" ? draft.days : draft.repeat === "once" ? "daily" : draft.repeat;

function DayPicker({ days, onChange }: { days: number[]; onChange: (days: number[]) => void }) {
  const toggle = (day: number) =>
    onChange(days.includes(day) ? days.filter(d => d !== day) : [...days, day].sort());
  return <div className="day-picker">
    {DAY_LABELS.map((label, day) =>
      <button type="button" key={label} aria-pressed={days.includes(day)}
        className={days.includes(day) ? "day on" : "day"}
        onClick={() => toggle(day)}>{label}</button>)}
  </div>;
}

function ScheduleForm({ draft, setDraft, onSubmit, onCancel, submitLabel, busy, error, agents, allowOnce }: {
  draft: Draft; setDraft: (d: Draft) => void; onSubmit: () => void; onCancel: () => void;
  submitLabel: string; busy: boolean; error: string; agents: string[]; allowOnce: boolean;
}) {
  // An empty or half-typed time field must not silently become midnight, which
  // is what `"".split(":").map(Number)` plus `|| 0` did.
  const setTime = (value: string) => {
    const [hour, minute] = value.split(":").map(Number);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return;
    setDraft({ ...draft, hour, minute });
  };
  // Switching to "weekly on selected days" with nothing selected leaves a rule
  // that matches no day, so it starts from today rather than from nothing.
  const setRepeat = (repeat: Repeat) =>
    setDraft({ ...draft, repeat, days: repeat === "custom" && !draft.days.length ? [new Date().getDay() === 0 ? 6 : new Date().getDay() - 1] : draft.days });
  const noDays = draft.repeat === "custom" && !draft.days.length;
  const options = allowOnce ? REPEAT_LABELS : REPEAT_LABELS.filter(([value]) => value !== "once");

  return <form className="schedule-form" onSubmit={event => { event.preventDefault(); onSubmit(); }}>
    <label>Name
      <input value={draft.label} placeholder="e.g. Morning stretch" autoFocus
        onChange={e => setDraft({ ...draft, label: e.target.value })}/>
    </label>
    {/* The name is a label - it titles the row and nothing else. This is the
        text north is actually sent at the scheduled time. */}
    <label>Prompt to run
      <textarea value={draft.task} rows={3} placeholder="e.g. remind me to stretch and log it"
        onChange={e => setDraft({ ...draft, task: e.target.value })}/>
    </label>
    <label>Repeats
      <select value={draft.repeat} onChange={e => setRepeat(e.target.value as Repeat)}>
        {options.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
      </select>
    </label>
    {draft.repeat === "custom" && <label>On these days
      <DayPicker days={draft.days} onChange={days => setDraft({ ...draft, days })}/>
    </label>}
    <div className="schedule-form-row">
      {/* A one-off happens on a date; a routine happens at a time, every time. */}
      {draft.repeat === "once" && <label>Date
        <input type="date" value={draft.date} min={todayISO()}
          onChange={e => e.target.value && setDraft({ ...draft, date: e.target.value })}/>
      </label>}
      <label>Time
        <input type="time" value={hhmm(draft.hour, draft.minute)} onChange={e => setTime(e.target.value)}/>
      </label>
      <label>Agent
        <select value={draft.agent} onChange={e => setDraft({ ...draft, agent: e.target.value })}>
          {(agents.includes(draft.agent) ? agents : [draft.agent, ...agents]).map(name =>
            <option value={name} key={name}>{name}</option>)}
        </select>
      </label>
    </div>
    {/* Beside the field that caused it, not at the top of the page. */}
    {error && <p className="schedule-form-error">{error}</p>}
    {noDays && <p className="schedule-form-error">Pick at least one day.</p>}
    <div className="schedule-form-actions">
      <button type="submit" className="primary-button" disabled={busy || !draft.task.trim() || noDays}>
        {busy ? "Saving…" : submitLabel}
      </button>
      <button type="button" className="ghost-button" onClick={onCancel}>Cancel</button>
    </div>
  </form>;
}

// An in-page confirmation. window.confirm is an operating-system dialog in an
// app that styles everything else itself, and it cannot say what will happen
// afterwards.
function ConfirmRow({ question, confirmLabel, onConfirm, onCancel, busy }: {
  question: string; confirmLabel: string; onConfirm: () => void; onCancel: () => void; busy: boolean;
}) {
  return <div className="schedule-confirm">
    <span>{question}</span>
    <div className="schedule-actions">
      <button className="ghost-button danger-link" onClick={onConfirm} disabled={busy}>{confirmLabel}</button>
      <button className="ghost-button" onClick={onCancel} disabled={busy}>Keep it</button>
    </div>
  </div>;
}

// A schedule north ships with. It is part of how north runs itself rather than
// something the user asked for, so the page shows it rather than offering to
// change it: the row says what it is for, and "Details" opens the whole
// definition. Listing these among the user's own routines made north's own
// housekeeping look like something they had set up and forgotten.
function BuiltinRow({ entry, restore }: { entry: Cron; restore?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const facts: [string, string][] = [
    ["Runs", entry.task],
    ["Agent", entry.agent],
    ["Repeats", `${entry.cadence} at ${hhmm(entry.hour, entry.minute)}`],
    ["Time zone", entry.tz],
    ["Next run", entry.enabled ? `${entry.next_run_local} (${whenFromNow(entry.next_run_epoch)})` : "paused"],
    ["Settings", entry.modified ? "changed from the ones north ships with" : "as north ships them"],
  ];
  return <div className={entry.enabled ? "schedule-row builtin" : "schedule-row builtin paused"}>
    <div className="schedule-main">
      <b>{entry.title}</b>
      {/* What it is for, in north's words - the part a built-in cannot say
          through its prompt, which for the nightly cleanup is a bare slug. */}
      {entry.description && <small className="schedule-description">{entry.description}</small>}
      <small>
        {entry.cadence} at {hhmm(entry.hour, entry.minute)} · {entry.agent}
        {entry.enabled ? ` · next ${entry.next_run_local}` : " · paused"}
        {entry.modified && " · edited"}
      </small>
      {open && <dl className="schedule-details">
        {facts.map(([term, value]) => <div key={term}><dt>{term}</dt><dd>{value}</dd></div>)}
      </dl>}
    </div>
    <div className="schedule-actions">
      <span className="schedule-locked">built-in</span>
      <button className="ghost-button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Details"}
      </button>
    </div>
    {restore}
  </div>;
}

export function Schedule() {
  const [editing, setEditing] = useState("");
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState("");
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Polling is paused while a form or confirmation is open: a list refreshing
  // under an open editor moves the row being edited.
  const settled = !editing && !creating && !confirming;
  const cron = useResource<Cron[]>("/orchestrator/cron", settled ? 10000 : 0);
  const agentList = useResource<{ name: string }[]>("/orchestrator/agents");
  // One query, partitioned here. Two queries on different intervals returned
  // two snapshots of the same job, so one that had just been requeued appeared
  // as "running now" and as pending work at the same time, in the same list.
  const jobs = useResource<Job[]>("/orchestrator/jobs?limit=50", settled ? 10000 : 0);

  // Every mutation runs through here so one place decides what happens on
  // failure: the message is shown and the lists are re-read, rather than a row
  // silently keeping whatever the optimistic guess was.
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await work();
      await Promise.all([cron.reload(), jobs.reload()]);
      setEditing("");
      setCreating(false);
      setConfirming("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const agents = (agentList.data || []).map(a => a.name).sort();
  const body = () => ({
    label: draft.label, task: draft.task, agent: draft.agent,
    hour: draft.hour, minute: draft.minute, days: daysField(draft),
  });
  // One form, two destinations. "Does not repeat" is an event on a date, which
  // is a job; anything else is a rule, which is a routine. Creating a one-off
  // needed the chat before this - the page could only make things that repeat.
  const create = () => act(() => draft.repeat === "once"
    ? post("/orchestrator/jobs", {
        agent: draft.agent,
        task: draft.task,
        payload: { scheduled_by: "schedule_page", label: draft.label },
        scheduled_at: `${draft.date}T${hhmm(draft.hour, draft.minute)}`,
      })
    : post("/orchestrator/cron", body()));
  const save = (name: string) => act(() => patch(`/orchestrator/cron/${encodeURIComponent(name)}`, body()));
  const setEnabled = (entry: Cron, enabled: boolean) =>
    act(() => patch(`/orchestrator/cron/${encodeURIComponent(entry.name)}`, { enabled }));
  const removeOrRestore = (entry: Cron) =>
    act(() => del(`/orchestrator/cron/${encodeURIComponent(entry.name)}`));
  const cancelJob = (job: Job) => act(() => del(`/orchestrator/jobs/${encodeURIComponent(job.job_id)}`));

  const close = () => { setEditing(""); setCreating(false); setConfirming(""); setError(""); };
  const startCreate = () => { close(); setDraft(emptyDraft()); setCreating(true); };
  const startEdit = (entry: Cron) => { close(); setDraft(draftOf(entry)); setEditing(entry.name); };

  const routines = cron.data || [];
  // Two lists, because they are two kinds of thing. A built-in is part of how
  // north runs itself; a routine is something the user (or north, on their
  // behalf) set up. Mixed together, north's own housekeeping read as a chore
  // the user had scheduled and could not remember scheduling.
  const mine = routines.filter(entry => entry.source !== "builtin");
  const builtins = routines.filter(entry => entry.source === "builtin");
  // A job carries only its prompt, so a firing of a named routine is titled by
  // the routine it came from - otherwise the same run reads two different ways
  // depending on which list it is in.
  const titleOf = (job: Job) =>
    job.label || routines.find(entry => entry.name === job.cron_entry)?.title || job.task;
  // A queued job that names a routine is that routine's next run, already
  // committed - not a separate one-off. Listed as both, a single firing showed
  // up twice: once as cancellable work and once as the routine's next time.
  const allJobs = jobs.data || [];
  const running = allJobs.filter(job => job.status === "running");
  const queued = allJobs.filter(job => job.status === "pending");
  const oneOffs = queued.filter(job => !job.cron_entry);
  // A routine with work already claimed or queued is represented by that job,
  // not by a second row predicting the same moment.
  const busyRoutines = new Set([...running, ...queued].map(job => job.cron_entry).filter(Boolean));
  // Only work that is over belongs under "Recently run" - a job still running
  // is not history, and appeared there as one.
  const finished = allJobs.filter(job => FINISHED.has(job.status)).slice(0, 6);
  // One timeline: a pending one-shot and a routine's next firing are the same
  // kind of fact - something north is going to do - and were split across two
  // panels that also disagreed about how to write a time.
  const upcoming = [
    ...running.map(job => ({
      key: job.job_id, task: job.task, agent: job.agent, epoch: 0,
      absolute: "started " + job.scheduled_local, kind: "running now" as const, job: undefined,
    })),
    ...queued.filter(job => job.cron_entry).map(job => ({
      key: job.job_id, task: titleOf(job), agent: job.agent, epoch: job.scheduled_epoch,
      absolute: job.scheduled_local, kind: "queued" as const, job: undefined,
    })),
    ...oneOffs.map(job => ({
      key: job.job_id, task: titleOf(job), agent: job.agent, epoch: job.scheduled_epoch,
      absolute: job.scheduled_local, kind: "once" as const, job,
    })),
    // A routine whose run is already queued is represented by that job, not by
    // a second row predicting the same moment.
    ...routines.filter(entry => entry.enabled && !busyRoutines.has(entry.name)).map(entry => ({
      key: entry.name, task: entry.title, agent: entry.agent, epoch: entry.next_run_epoch,
      absolute: entry.next_run_local, kind: entry.cadence, job: undefined,
    })),
  ].sort((a, b) => a.epoch - b.epoch);

  return <div className="page">
    {/* The action belongs to the page, not to one panel: it makes a one-off
        event or a repeating routine, and those land in different lists. */}
    <PageHeader eyebrow="Automation" title="Schedule" subtitle="What north will do next, and the routines behind it."
      actions={<button className="primary-button" onClick={startCreate}>+ Schedule something</button>}/>
    {creating && <Panel title={draft.repeat === "once" ? "New one-off" : "New routine"} label="new">
      <ScheduleForm draft={draft} setDraft={setDraft} onSubmit={create} onCancel={close}
        submitLabel={draft.repeat === "once" ? "Schedule it" : "Create routine"}
        busy={busy} error={error} agents={agents} allowOnce/>
    </Panel>}
    {(cron.error || jobs.error) && <ErrorNotice message={cron.error || jobs.error}/>}

    <Panel title="Next up" label="soonest first">
      {cron.loading || jobs.loading ? <Loading/> : upcoming.length ? upcoming.slice(0, 8).map(item =>
        <div className="schedule-row upcoming" key={item.key}>
          <div className="schedule-main">
            <b>{item.task}</b>
            <small title={item.absolute}>{whenFromNow(item.epoch)} · {item.absolute} · {item.kind} · {item.agent}</small>
          </div>
          {item.job
            ? confirming === item.key
              ? <ConfirmRow question="Cancel this one-off?" confirmLabel="Cancel it" busy={busy}
                  onConfirm={() => cancelJob(item.job!)} onCancel={() => setConfirming("")}/>
              : <div className="schedule-actions">
                  <button className="ghost-button" onClick={() => setConfirming(item.key)} disabled={busy}>Cancel</button>
                </div>
            : <span className="schedule-upcoming-kind">from a routine</span>}
        </div>) : <Empty>Nothing is scheduled. Add a routine below, or ask north to remind you about something.</Empty>}
    </Panel>

    <Panel title="Your routines" label={`${mine.length} recurring`}>
      {cron.loading ? <Loading/> : mine.length ? mine.map(entry => (
        <div className={entry.enabled ? "schedule-row" : "schedule-row paused"} key={entry.name}>
          <div className="schedule-main">
            <b>{entry.title}</b>
            <small>
              {entry.cadence} at {hhmm(entry.hour, entry.minute)} · {entry.agent}
              {entry.enabled ? ` · next ${entry.next_run_local}` : " · paused"}
            </small>
            {/* Once the title is a name, what actually runs is no longer on
                screen - and that is the part worth being able to check. */}
            {entry.label && <small className="schedule-prompt">runs: {entry.task}</small>}
          </div>
          {confirming === entry.name
            ? <ConfirmRow busy={busy} onCancel={() => setConfirming("")} onConfirm={() => removeOrRestore(entry)}
                question="Delete this routine? It will not run again." confirmLabel="Delete"/>
            : <div className="schedule-actions">
                <button className="ghost-button" onClick={() => setEnabled(entry, !entry.enabled)} disabled={busy}>
                  {entry.enabled ? "Pause" : "Resume"}
                </button>
                <button className="ghost-button" onClick={() => startEdit(entry)} disabled={busy}>Edit</button>
                <button className="ghost-button danger-link" onClick={() => setConfirming(entry.name)} disabled={busy}>Delete</button>
              </div>}
          {/* A routine cannot become a one-off in place - that is a delete and
              a new event - so "Does not repeat" is not offered when editing. */}
          {editing === entry.name && <ScheduleForm draft={draft} setDraft={setDraft} onCancel={close}
            onSubmit={() => save(entry.name)} submitLabel="Save changes" busy={busy} error={error}
            agents={agents} allowOnce={false}/>}
        </div>
      )) : <Empty>No routines of your own yet. Schedule something above, or just ask north for it.</Empty>}
    </Panel>

    {/* Read-only: these are north's own, not the user's, and changing one
        changes how north runs rather than what it does for them. The one
        exception is undoing an edit made before they were read-only, which
        would otherwise be stranded with no way back to the shipped values. */}
    <Panel title="Built into north" label="read-only">
      {cron.loading ? <Loading/> : builtins.length ? builtins.map(entry =>
        <BuiltinRow key={entry.name} entry={entry} restore={entry.modified && (
          confirming === entry.name
            ? <div className="schedule-restore">
                <ConfirmRow busy={busy} onCancel={() => setConfirming("")} onConfirm={() => removeOrRestore(entry)}
                  question="Put this back to the settings north ships with?" confirmLabel="Restore"/>
              </div>
            : <div className="schedule-restore">
                <button className="ghost-button" onClick={() => setConfirming(entry.name)} disabled={busy}>
                  Restore default
                </button>
              </div>)}/>
      ) : <Empty>North has no built-in schedules.</Empty>}
    </Panel>

    <Panel title="Recently run" label="history">
      {finished.length ? finished.map(job =>
        <div className="list-row" key={job.job_id}>
          <div><b>{titleOf(job)}</b><small>{job.agent} · {job.scheduled_local}</small></div>
          <Status value={job.status}/>
        </div>) : <Empty>Nothing has run yet.</Empty>}
    </Panel>
  </div>;
}

const docs = ["user.md", "north_stars.md", "judgement_rules.md", "soul.md"];
const docLabels: Record<string, string> = {
  "user.md": "User facts",
  "north_stars.md": "North stars",
  "judgement_rules.md": "Judgement rules",
  "soul.md": "Soul",
};
interface ContextDoc { document: string; content: string; }
// Everything north has learned, in one place. Four kinds of memory that lived in
// four different states of visibility: the documents were editable here, the
// facts were listed below them, and the episodes and approval decisions had no
// endpoint at all - north was learning things nobody could look at.
type MemoryTab = "documents" | "facts" | "episodes" | "approvals" | "rules";

const MEMORY_TABS: [MemoryTab, string][] = [
  ["documents", "Documents"],
  ["facts", "Facts"],
  ["episodes", "Episodes"],
  ["approvals", "Approvals"],
  ["rules", "Safe actions"],
];

interface Episode { id: string; task_id: string; domain: string; outcome: string; summary: string; timestamp: string; }
interface ApprovalDecision {
  fingerprint: string; agent: string; signature: string; decision: string; count: number; updated_at: string;
}

// What north replays instead of asking, once autonomy is turned up. Shown so it
// can be checked before it is trusted, and forgotten one row at a time - a
// decision that cannot be withdrawn is not consent.
function ApprovalMemoryPanel() {
  const resource = useResource<ApprovalDecision[]>("/web/api/memory/approvals", 10000);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const forget = async (row: ApprovalDecision) => {
    setBusy(row.fingerprint);
    setError("");
    try { await del(`/web/api/memory/approvals/${encodeURIComponent(row.fingerprint)}`); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(""); }
  };
  const rows = resource.data || [];
  return <Panel title="Learned decisions" label={`${rows.length} remembered`}>
    {error && <ErrorNotice message={error}/>}
    <p className="muted memory-note">
      Every time you approve or reject an action, north records it against a fingerprint of that
      action and replays your decision when a matching one comes up. This is what autonomous mode
      runs on.
    </p>
    {resource.loading ? <Loading/> : rows.length ? rows.map(row =>
      <div className="memory-row" key={row.fingerprint}>
        <div className="memory-main">
          <b>{row.signature}</b>
          <small>
            {row.agent} · <span className={`decision decision-${row.decision}`}>{row.decision}</span>
            {row.count > 1 && ` · ${row.count}×`} · {timeAgo(row.updated_at)}
          </small>
        </div>
        <button className="ghost-button danger-link" disabled={busy === row.fingerprint}
          onClick={() => forget(row)}>Forget</button>
      </div>) : <Empty>Nothing learned yet. Approve or reject an action and it appears here.</Empty>}
  </Panel>;
}

interface UnattendedRule {
  id: string; kind: string; pattern: string; enabled: boolean; source: string;
  note: string; fire_count: number; last_fired_at: string | null;
}
interface UnattendedRules { mode: string; active: boolean; kinds: string[]; rules: UnattendedRule[]; }

const KIND_LABELS: Record<string, string> = {
  command: "Commands",
  git: "Git actions",
  device: "Device toggles",
  self_message: "Messages to you",
};

const KIND_HELP: Record<string, string> = {
  command: "Run without asking. Matched as a whole command or a prefix; chaining, pipes and redirects are always refused.",
  git: "Local, reversible git only. Push, pull, merge and forced variants are never auto-approved.",
  device: "Trivially reversible physical actions, where the undo is another toggle.",
  self_message: "Messages addressed to you. A message to anyone else is never auto-approved.",
};

// The safe-action list north runs in `auto` mode without asking. It used to be a
// tuple in a source file: you could not see it, add to it, or take anything out
// of it. approval_memory next door already settled the principle - a decision
// that cannot be withdrawn is not consent - and a hardcoded allowlist fails it.
function SafeActionsPanel() {
  const resource = useResource<UnattendedRules>("/web/api/unattended/rules", 10000);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [kind, setKind] = useState("command");
  const [pattern, setPattern] = useState("");

  const run = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id); setError("");
    try { await fn(); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(""); }
  };
  const toggle = (row: UnattendedRule) =>
    run(row.id, () => patch(`/web/api/unattended/rules/${encodeURIComponent(row.id)}`, { enabled: !row.enabled }));
  const remove = (row: UnattendedRule) => {
    const shipped = row.source === "builtin";
    const question = shipped
      ? `Disable the shipped rule "${row.pattern}"? It stays listed so you can restore it.`
      : `Delete the rule "${row.pattern}"?`;
    if (!window.confirm(question)) return;
    return run(row.id, () => del(`/web/api/unattended/rules/${encodeURIComponent(row.id)}`));
  };
  const add = () => {
    if (!pattern.trim()) return;
    return run("new", async () => { await post("/web/api/unattended/rules", { kind, pattern: pattern.trim() }); setPattern(""); });
  };
  const restore = () => run("restore", () => post("/web/api/unattended/rules/restore", {}));

  const data = resource.data;
  const rules = data?.rules || [];
  const kinds = data?.kinds || Object.keys(KIND_LABELS);

  return <Panel title="Safe actions" label={`${rules.filter(r => r.enabled).length} active`}
    actions={<button className="ghost-button" disabled={busy === "restore"} onClick={restore}>Restore shipped rules</button>}>
    {error && <ErrorNotice message={error}/>}

    {/* A rule only ever fires in `auto`. Listing rules that cannot fire, with
        nothing saying so, is how someone concludes the feature is broken. */}
    {data && !data.active && <div className="notice">
      These rules are inactive: north is in <b>{data.mode}</b> mode and asks about everything.
      They apply in <b>auto</b> mode.
    </div>}

    <p className="muted memory-note">
      What north may do without asking, in auto mode. Two things are never on this list, whatever you
      add: sending something to another person, and spending money. A sent email cannot be unsent.
    </p>

    <div className="fact-editor">
      <select value={kind} onChange={e => setKind(e.target.value)}>
        {kinds.map(k => <option key={k} value={k}>{KIND_LABELS[k] || k}</option>)}
      </select>
      <input value={pattern} onChange={e => setPattern(e.target.value)}
        placeholder={kind === "command" ? "e.g. make lint" : "e.g. fetch"} onKeyDown={e => { if (e.key === "Enter") add(); }}/>
      <button className="ghost-button" disabled={busy === "new"} onClick={add}>Add rule</button>
    </div>

    {resource.loading ? <Loading/> : kinds.map(k => {
      const forKind = rules.filter(r => r.kind === k);
      if (!forKind.length) return null;
      return <div className="rule-group" key={k}>
        <div className="editor-label">{KIND_LABELS[k] || k}</div>
        <p className="muted memory-note">{KIND_HELP[k]}</p>
        {forKind.map(row => <div className={`memory-row ${row.enabled ? "" : "rule-disabled"}`} key={row.id}>
          <div className="memory-main">
            <b>{row.pattern}</b>
            <small>
              {row.source === "builtin" ? "shipped" : "yours"}
              {!row.enabled && " · disabled"}
              {/* A rule that has fired 40 times is a different object from one
                  that never has, and that is what you want to see before keeping it. */}
              {row.fire_count > 0
                ? ` · used ${row.fire_count}×${row.last_fired_at ? `, last ${timeAgo(row.last_fired_at)}` : ""}`
                : " · never used"}
            </small>
          </div>
          <div className="fact-actions">
            <button disabled={busy === row.id} onClick={() => toggle(row)}>{row.enabled ? "Disable" : "Enable"}</button>
            <button className="danger-link" disabled={busy === row.id} onClick={() => remove(row)}>
              {row.source === "builtin" ? "Remove" : "Delete"}
            </button>
          </div>
        </div>)}
      </div>;
    })}
    {!resource.loading && !rules.length && <Empty>No safe actions configured.</Empty>}
  </Panel>;
}

// A record of what happened, so read-only: the honest way to change an episode
// is to do the thing differently, not to edit the note.
function EpisodesPanel() {
  const resource = useResource<Episode[]>("/web/api/memory/episodes", 10000);
  const rows = resource.data || [];
  return <Panel title="Episodes" label={`${rows.length} remembered`}>
    <p className="muted memory-note">
      What north remembers of past tasks, used to recognise a situation it has been in before.
    </p>
    {resource.loading ? <Loading/> : rows.length ? rows.map(row =>
      <div className="memory-row" key={row.id}>
        <div className="memory-main">
          <b>{row.summary}</b>
          <small>{row.domain} · {row.task_id} · {timeAgo(row.timestamp)}</small>
        </div>
        <Status value={row.outcome}/>
      </div>) : <Empty>No episodes recorded yet.</Empty>}
  </Panel>;
}

export function Memory() {
  const [tab, setTab] = useState<MemoryTab>("documents");
  const [doc, setDoc] = useState(docs[0]);
  const resource = useResource<ContextDoc>(`/orchestrator/context/${doc}`);
  const facts = useResource<any[]>("/web/api/memory/facts", 10000);
  const [draft, setDraft] = useState<string | null>(null);
  const [factDraft, setFactDraft] = useState("");
  const [editingFact, setEditingFact] = useState<string | null>(null);
  const isUserFacts = doc === "user.md";
  const content = isUserFacts ? "" : (draft ?? resource.data?.content ?? "");
  const save = async () => { await api(`/orchestrator/context/${doc}`, { method: "PUT", body: JSON.stringify({ content }) }); setDraft(null); await resource.reload(); };
  const removeDocument = async () => { if (!window.confirm(`Reset ${docLabels[doc]}?`)) return; await api(`/orchestrator/context/${doc}`, { method: "DELETE" }); setDraft(null); await resource.reload(); };
  const saveFact = async () => { if (!factDraft.trim()) return; if (editingFact) await api(`/web/api/memory/facts/${editingFact}`, { method: "PATCH", body: JSON.stringify({ content: factDraft, category: "user" }) }); else await post("/web/api/memory/facts", { content: factDraft, category: "user" }); setFactDraft(""); setEditingFact(null); await facts.reload(); };
  const removeFact = async (id: string) => { if (!window.confirm("Delete this fact?")) return; await api(`/web/api/memory/facts/${id}`, { method: "DELETE" }); await facts.reload(); };
  const onDocuments = tab === "documents";
  return <div className="page">
    <PageHeader eyebrow="Knowledge" title="Memory" subtitle="Everything north has learned about you, and from you."
      actions={onDocuments && !isUserFacts
        ? <><button onClick={removeDocument}>Reset document</button>
            <button className="primary-button" disabled={draft === null} onClick={save}>Save document</button></>
        : null}/>
    <div className="segmented memory-tabs">
      {MEMORY_TABS.map(([value, label]) =>
        <button className={tab === value ? "active" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}
    </div>

    {onDocuments && <div className="memory-layout"><aside>{docs.map(name => <button className={doc === name ? "active" : ""} key={name} onClick={() => { setDoc(name); setDraft(null); }}>{docLabels[name]}</button>)}</aside><div className="memory-editor-grid"><section><div className="editor-label">{isUserFacts ? "User facts" : "Markdown source"}</div>{isUserFacts ? <div className="document-help">User details are managed as individual facts on the Facts tab.</div> : resource.loading ? <Loading/> : <textarea className="document-editor" value={content} onChange={e => setDraft(e.target.value)} />}</section><section className={`memory-preview ${isUserFacts ? "user-facts-preview" : ""}`}><div className="editor-label">Rendered preview</div>{isUserFacts ? <div className="document-help">Your durable user details live on the Facts tab.</div> : <Markdown>{content || "Nothing written yet."}</Markdown>}</section></div></div>}

    {tab === "facts" && <section className="facts-panel panel"><header><div><span>Durable context</span><h2>Facts</h2></div><span>{facts.data?.length || 0} stored</span></header><div className="panel-content"><div className="fact-editor"><input value={factDraft} onChange={e => setFactDraft(e.target.value)} placeholder="Add one atomic fact…"/><button className="ghost-button" onClick={saveFact}>{editingFact ? "Update" : "Add fact"}</button>{editingFact && <button className="ghost-button" onClick={() => { setEditingFact(null); setFactDraft(""); }}>Cancel</button>}</div><div className="fact-list">{facts.loading ? <Loading/> : (facts.data || []).map(fact => <article className="fact-row" key={fact.id}><div><b>{fact.content}</b><small>{fact.category || "general"} · updated {timeAgo(fact.updated_at)}</small></div><div className="fact-actions"><span>{fact.confidence != null ? `${Math.round(Number(fact.confidence) * 100)}%` : "Active"}</span><button onClick={() => { setEditingFact(fact.id); setFactDraft(fact.content); }}>Edit</button><button onClick={() => removeFact(fact.id)}>Delete</button></div></article>)}{!facts.loading && !facts.data?.length && <Empty>No durable facts have been captured yet.</Empty>}</div></div></section>}

    {tab === "episodes" && <EpisodesPanel/>}
    {tab === "approvals" && <ApprovalMemoryPanel/>}
    {tab === "rules" && <SafeActionsPanel/>}
    {onDocuments && <Bootstrap embedded/>}
  </div>;
}

interface Agent { name: string; domain: string; model_pool: string; accepts: string[]; }
interface Confidence { agent: string; tool: string; confidence: number; }
export function Agents() {
  const agents = useResource<Agent[]>("/orchestrator/agents");
  const confidence = useResource<Confidence[]>("/orchestrator/tools/confidence");
  return <div className="page"><PageHeader eyebrow="Capabilities" title="Agents" subtitle="North's specialist team and the tools they trust."/><div className="agent-grid">{agents.data?.map(agent => <article key={agent.name}><div className="agent-avatar">{agent.name.slice(0,1).toUpperCase()}</div><h2>{agent.name}</h2><p>{agent.domain} · {agent.model_pool}</p><div className="tag-list">{agent.accepts.slice(0,5).map(item => <span key={item}>{item}</span>)}</div><h3>Tool confidence</h3>{confidence.data?.filter(item => item.agent === agent.name).slice(0,4).map(item => <div className="confidence" key={item.tool}><span>{item.tool}</span><i><b style={{width: `${item.confidence * 100}%`}}/></i></div>)}</article>)}</div></div>;
}

interface SkillSummary { name: string; description: string; source: string; version: string; status: string; domains: string[]; }
interface SkillDetail { name: string; content: string; source: string; }
export function Skills() {
  const skills = useResource<SkillSummary[]>("/web/api/skills", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [message, setMessage] = useState("");
  const open = async (name: string) => { try { const detail = await api<SkillDetail>(`/web/api/skills/${name}`); setSelected(name); setContent(detail.content); setMessage(""); } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); } };
  const save = async () => { if (!selected) return; try { await api(`/web/api/skills/${selected}`, { method: "PUT", body: JSON.stringify({ content }) }); setMessage("Skill saved and reloaded."); await skills.reload(); } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); } };
  return <div className="page"><PageHeader eyebrow="Procedures" title="Skills" subtitle="Inspect and edit the playbooks North injects into specialist work." actions={<button className="primary-button" disabled={!selected} onClick={save}>Save skill</button>}/>{skills.error && <ErrorNotice message={skills.error}/>} {message && <div className="notice">{message}</div>}<div className="skills-layout"><div className="skill-library">{skills.loading ? <Loading/> : (skills.data || []).map(skill => <button className={selected === skill.name ? "active" : ""} key={skill.name} onClick={() => open(skill.name)}><b>{skill.name}</b><small>{skill.source} · v{skill.version} · {skill.domains.join(", ")}</small><p>{skill.description}</p></button>)}</div><section className="skill-editor">{selected ? <><div className="editor-label">{selected}/SKILL.md</div><textarea value={content} onChange={event => setContent(event.target.value)}/></> : <Empty>Select a skill to inspect or edit it.</Empty>}</section></div></div>;
}

export function Activity() {
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=300", 5000);
  const [query, setQuery] = useState("");
  const rows = useMemo(() => (resource.data || []).filter(item => JSON.stringify(item).toLowerCase().includes(query.toLowerCase())), [resource.data, query]);
  return <div className="page"><PageHeader eyebrow="Audit trail" title="Activity" subtitle="Every important action and state transition across North." actions={<input className="header-search" placeholder="Filter events" value={query} onChange={e => setQuery(e.target.value)}/>}/>{resource.loading ? <Loading/> : <div className="event-list verbose-events">{rows.map(entry => <div className="event-row" key={entry.id}><span className={`event-dot ${entry.status || ""}`}/><div><b>{entry.action?.replaceAll("_", " ") || entry.source}</b><small>{entry.agent || entry.source} · {entry.task_id || "system"} · {new Date(entry.timestamp).toLocaleString()}</small>{(entry.output || entry.input) && <p>{(entry.output || entry.input || "").slice(0,600)}</p>}</div><Status value={entry.status}/></div>)}</div>}</div>;
}

export function Insights() {
  const metrics = useResource<Record<string, any>>("/orchestrator/metrics?days=30", 15000);
  const costs = useResource<Record<string, any>>("/orchestrator/inference/costs?period=month", 15000);
  const models = useResource<Record<string, any>>("/orchestrator/inference/models", 15000);
  return <div className="page"><PageHeader eyebrow="Performance" title="Insights" subtitle="Usage, cost, reliability, and model availability."/><div className="metric-cards"><div><span>Tasks · 30 days</span><strong>{metrics.data?.total_tasks || 0}</strong></div><div><span>Input tokens</span><strong>{Number(metrics.data?.total_tokens_in || 0).toLocaleString()}</strong></div><div><span>Output tokens</span><strong>{Number(metrics.data?.total_tokens_out || 0).toLocaleString()}</strong></div><div><span>Model cost</span><strong>${Number(costs.data?.total_cost_usd || 0).toFixed(4)}</strong></div></div><div className="two-column"><Panel title="Cost by model">{Object.entries(costs.data?.by_model || {}).map(([name,value]) => <div className="list-row" key={name}><b>{name}</b><span>${Number(value).toFixed(4)}</span></div>)}</Panel><Panel title="Model pools">{Object.entries(models.data || {}).map(([name, pool]: [string, any]) => <div className="list-row" key={name}><div><b>{name}</b><small>{pool.models?.length || 0} models available</small></div><span className="pool-availability">Available</span></div>)}</Panel></div></div>;
}

interface SettingsData { power: string; autonomy: string; routing: string; model: string; }
interface ProviderModels { provider: string; models: string[]; }

// Which model answers, when the user is choosing it rather than north. Provider
// first, then that provider's models: a flat list of several hundred ids is not
// a choice anyone can make, and the provider is the half people know.
function ModelPicker({ value, onPick, busy }: { value: string; onPick: (spec: string) => void; busy: boolean }) {
  const catalog = useResource<ProviderModels[]>("/orchestrator/inference/catalog");
  const providers = catalog.data || [];
  // A stored pin is "provider:model_id", but only the provider half is a fixed
  // vocabulary - a model id may itself contain a colon, so it is split once.
  const [chosenProvider, chosenModel] = (() => {
    const at = value.indexOf(":");
    return at > 0 ? [value.slice(0, at), value.slice(at + 1)] : ["", value];
  })();
  const provider = chosenProvider || providers[0]?.provider || "";
  const models = providers.find(row => row.provider === provider)?.models || [];

  if (catalog.loading) return <Loading/>;
  if (catalog.error) return <ErrorNotice message={`Could not read the model catalog: ${catalog.error}`}/>;
  if (!providers.length) return <Empty>No models are reachable. Check your provider keys under System.</Empty>;
  return <div className="model-picker">
    <label>Provider
      <select value={provider} disabled={busy}
        onChange={event => onPick(`${event.target.value}:${(providers.find(r => r.provider === event.target.value)?.models || [])[0] || ""}`)}>
        {providers.map(row => <option value={row.provider} key={row.provider}>{row.provider}</option>)}
      </select>
    </label>
    <label>Model
      <select value={chosenModel} disabled={busy || !models.length}
        onChange={event => onPick(`${provider}:${event.target.value}`)}>
        {(models.includes(chosenModel) || !chosenModel ? models : [chosenModel, ...models]).map(id =>
          <option value={id} key={id}>{id}</option>)}
      </select>
    </label>
  </div>;
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [typeScale, setTypeScale] = useState(() => localStorage.getItem("north-type-scale") || "comfortable");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { document.documentElement.dataset.typeScale = typeScale; localStorage.setItem("north-type-scale", typeScale); }, [typeScale]);
  const update = async (body: Partial<SettingsData>) => {
    setBusy(true);
    setError("");
    try { await post("/orchestrator/settings", body); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  const routing = resource.data?.routing || "auto";
  const manual = routing === "manual";
  // Manual routing needs a model, and the server refuses the switch without one.
  // So choosing "manual" with nothing stored opens the picker and waits: the
  // switch is made by picking a model, which is the decision anyway.
  const [choosing, setChoosing] = useState(false);
  const pickManual = () => (resource.data?.model ? update({ routing: "manual" }) : setChoosing(true));
  return <div className="page">
    <PageHeader eyebrow="Configuration" title="Settings"
      subtitle="Control how North balances capability, cost, autonomy, and readability."/>
    {error && <ErrorNotice message={error}/>}
    <div className="settings-grid">
      <Panel title="Model routing" label="Who picks">
        <div className="segmented two">
          <button className={!manual && !choosing ? "active" : ""} disabled={busy}
            onClick={() => { setChoosing(false); update({ routing: "auto" }); }}>auto</button>
          <button className={manual || choosing ? "active" : ""} disabled={busy} onClick={pickManual}>manual</button>
        </div>
        <p className="muted">
          {manual
            ? "One model answers everything. North's ranking is skipped, and a model it cannot reach fails the call rather than falling back."
            : choosing
              ? "Pick the model that should answer everything."
              : "North ranks every model it can reach against what each part of a task needs, and calls them in that order."}
        </p>
        {(manual || choosing) && <ModelPicker value={resource.data?.model || ""} busy={busy}
          onPick={spec => { setChoosing(false); update({ routing: "manual", model: spec }); }}/>}
      </Panel>
      {/* Ordering a chain of one is meaningless, so under manual the dial is
          shown switched off rather than left looking live. */}
      <Panel title="Power" label={manual ? "not in use" : "Model strategy"} className={manual ? "panel-inert" : ""}>
        <div className="segmented">
          {["eco","cruise","sport"].map(value =>
            <button className={resource.data?.power === value ? "active" : ""} disabled={busy || manual}
              onClick={() => update({ power: value })} key={value}>{value}</button>)}
        </div>
        <p className="muted">
          {manual
            ? "Nothing to order while one model is pinned. Switch routing to auto to use this."
            : "Choose how aggressively North selects capable models."}
        </p>
      </Panel>
      <Panel title="Autonomy" label="Approval behavior">
        <div className="segmented">
          {["interactive","auto","autonomous"].map(value =>
            <button className={resource.data?.autonomy === value ? "active" : ""} disabled={busy}
              onClick={() => update({ autonomy: value })} key={value}>{value}</button>)}
        </div>
        <p className="muted">Consequential and destructive actions remain governed by North's safety policy.</p>
      </Panel>
      <Panel title="Text size" label="Personal preference">
        <div className="segmented text-scale-selector">
          {[["compact","Compact"],["comfortable","Comfortable"],["large","Large"]].map(([value,label]) =>
            <button className={typeScale === value ? "active" : ""} onClick={() => setTypeScale(value)} key={value}>{label}</button>)}
        </div>
        <p className="muted">Choose the reading scale used throughout the web interface.</p>
      </Panel>
    </div>
  </div>;
}

interface ProviderAuthState {
  provider_id: string;
  state: "starting" | "pending" | "connected" | "disconnected" | "cancelled" | "error";
  configured: boolean;
  detail: string;
  authorization_url?: string;
  account_hint?: string;
}

// The embeddings row is the only provider with no key and no login, so it is
// the only one whose identifying detail has to come from elsewhere.
function isLocalEmbeddings(provider: any) {
  return !provider.env_key && provider.auth_kind !== "oauth_pkce";
}

export function SystemPage() {
  const overview = useResource<any>("/web/api/system", 8000);
  const metrics = useResource<any>("/orchestrator/metrics?days=30", 15000);
  const costs = useResource<any>("/orchestrator/inference/costs?period=month", 15000);
  const models = useResource<Record<string, any>>("/orchestrator/inference/models", 15000);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [providerMessage, setProviderMessage] = useState("");
  const [providerAuth, setProviderAuth] = useState<Record<string, ProviderAuthState>>({});
  const [expandedPool, setExpandedPool] = useState<string | null>(null);
  const authWindows = useRef<Record<string, Window | null>>({});
  const providers = overview.data?.providers || [];
  const embeddings = overview.data?.embeddings;
  const embeddingsFor = (provider: any) =>
    isLocalEmbeddings(provider) && embeddings?.model ? embeddings.model : "";
  const totalCost = Number(costs.data?.total_cost_usd ?? metrics.data?.total_cost_usd ?? 0);
  const pendingAuthIds = Object.values(providerAuth)
    .filter(state => state.state === "starting" || state.state === "pending")
    .map(state => state.provider_id)
    .sort()
    .join(",");

  const saveProvider = async (id: string) => {
    try {
      await post(`/web/api/providers/${id}`, { api_key: keys[id] });
      setKeys(current => ({ ...current, [id]: "" }));
      setProviderMessage("Provider credentials saved and runtime refreshed.");
      await overview.reload();
    } catch (error) {
      setProviderMessage(String(error));
    }
  };

  const connectProvider = async (id: string) => {
    const popup = window.open("about:blank", `north-${id}-auth`, "popup,width=560,height=760");
    authWindows.current[id] = popup;
    if (popup) popup.document.body.textContent = "Preparing secure OpenAI login…";
    setProviderAuth(current => ({
      ...current,
      [id]: { provider_id: id, state: "starting", configured: false, detail: "Preparing browser login…" },
    }));
    try {
      const state = await post<ProviderAuthState>(`/web/api/providers/${id}/auth`, {});
      setProviderAuth(current => ({ ...current, [id]: state }));
      if (state.authorization_url && popup) {
        popup.opener = null;
        popup.location.replace(state.authorization_url);
      } else if (state.authorization_url) {
        setProviderMessage("Pop-up blocked. Use ‘Continue login’ below.");
      } else if (popup) {
        popup.close();
      }
      if (state.state === "connected") await overview.reload();
    } catch (error) {
      popup?.close();
      setProviderAuth(current => ({
        ...current,
        [id]: { provider_id: id, state: "error", configured: false, detail: String(error) },
      }));
    }
  };

  const disconnectProvider = async (id: string) => {
    try {
      const state = await api<ProviderAuthState>(`/web/api/providers/${id}/auth`, { method: "DELETE" });
      setProviderAuth(current => ({ ...current, [id]: state }));
      setProviderMessage("OpenAI Codex disconnected.");
      await overview.reload();
    } catch (error) {
      setProviderMessage(String(error));
    }
  };

  useEffect(() => {
    const ids = pendingAuthIds ? pendingAuthIds.split(",") : [];
    if (!ids.length) return;
    let stopped = false;
    const poll = async () => {
      for (const id of ids) {
        try {
          const state = await api<ProviderAuthState>(`/web/api/providers/${id}/auth`);
          if (stopped) return;
          setProviderAuth(current => ({ ...current, [id]: state }));
          if (!["starting", "pending"].includes(state.state)) {
            authWindows.current[id]?.close();
            delete authWindows.current[id];
            await overview.reload();
          }
        } catch (error) {
          if (!stopped) setProviderMessage(String(error));
        }
      }
    };
    const timer = window.setInterval(poll, 1500);
    void poll();
    return () => { stopped = true; window.clearInterval(timer); };
  }, [pendingAuthIds]);

  return <div className="page">
    <PageHeader eyebrow="Runtime" title="System" subtitle="Every detail about North's runtime, providers, model pools, costs, and health."/>
    <div className="system-hero"><HealthIndicator variant="hero"/></div>
    <div className="metric-cards">
      <div><span>Tasks · 30 days</span><strong>{metrics.data?.total_tasks || 0}</strong></div>
      <div><span>Input tokens</span><strong>{Number(metrics.data?.total_tokens_in || 0).toLocaleString()}</strong></div>
      <div><span>Output tokens</span><strong>{Number(metrics.data?.total_tokens_out || 0).toLocaleString()}</strong></div>
      <div><span>Model cost · month</span><strong>${totalCost.toFixed(4)}</strong></div>
    </div>
    <div className="two-column">
      <Panel title="Providers" label={`${providers.filter((provider: any) => provider.configured).length} configured`}>
        {providerMessage && <div className="notice">{providerMessage}</div>}
        <div className="provider-list">{providers.map((provider: any) => {
          const auth = providerAuth[provider.id];
          const waiting = auth?.state === "starting" || auth?.state === "pending";
          const configured = auth ? auth.configured : provider.configured;
          return <div className="provider-row" key={provider.id}>
            {/* The trailing detail is whatever identifies the provider: the env var
                holding its key, "Browser login" for OAuth, or - for the on-device
                embedder, which has no key at all - the model it runs. Without the
                last case the row ended on a dangling separator. */}
            <div><b>{provider.name}</b><small>{[provider.description, provider.auth_kind === "oauth_pkce" ? "Browser login" : provider.env_key || embeddingsFor(provider)].filter(Boolean).join(" · ")}</small></div>
            <div className="provider-controls">
              <span className={configured ? "provider-state configured" : "provider-state"}>
                {waiting ? "Waiting for browser login" : configured ? `Ready ${auth?.account_hint || provider.credential_hint}` : "Not configured"}
              </span>
              {provider.env_key && <div className="provider-edit">
                <input type="password" placeholder="API key" value={keys[provider.id] || ""} onChange={event => setKeys(current => ({ ...current, [provider.id]: event.target.value }))}/>
                <button className="ghost-button" disabled={!keys[provider.id]} onClick={() => saveProvider(provider.id)}>Save</button>
              </div>}
              {provider.auth_kind === "oauth_pkce" && <div className="provider-auth">
                <div className="provider-auth-actions">
                  <button className={configured ? "ghost-button" : "primary-button"} disabled={waiting} onClick={() => connectProvider(provider.id)}>{waiting ? "Waiting…" : configured ? "Reconnect" : "Connect"}</button>
                  {configured && <button className="ghost-button" onClick={() => disconnectProvider(provider.id)}>Disconnect</button>}
                  {auth?.authorization_url && <a href={auth.authorization_url} target="_blank" rel="noreferrer">Continue login</a>}
                </div>
                {auth?.detail && <small className={auth.state === "error" ? "provider-auth-error" : "provider-auth-detail"}>{auth.detail}</small>}
              </div>}
            </div>
          </div>;
        })}</div>
      </Panel>
      <Panel title="Model pools" label="Available now"><div>{Object.entries(models.data || {}).map(([name, pool]: [string, any]) => <div className="model-pool" key={name}><button className="model-pool-toggle" onClick={() => setExpandedPool(expandedPool === name ? null : name)}><span><b>{name}</b><small>{pool.models?.length || 0} models available</small></span><span className="pool-availability">{expandedPool === name ? "Hide" : "Inspect"}</span></button>{expandedPool === name && <div className="model-list">{(pool.models || []).map((model: any) => <div className="model-row" key={`${model.provider}-${model.id}`}><b>{model.id}</b><span>{model.provider}</span></div>)}</div>}</div>)}</div></Panel>
    </div>
    <div className="two-column">
      <Panel title="Cost by model" label="Month to date">{Object.entries(costs.data?.by_model || {}).map(([name,value]) => <div className="list-row" key={name}><b>{name}</b><span>${Number(value).toFixed(4)}</span></div>)}{!Object.keys(costs.data?.by_model || {}).length && <Empty>No recorded inference costs yet.</Empty>}</Panel>
      <Panel title="Runtime configuration">
        <div className="list-row"><b>Routing</b><span>{overview.data?.settings?.routing || "–"}</span></div>
        {/* Only shown when it is in force: a remembered pin that auto has released
            would read here as the model north is using, which it is not. */}
        {overview.data?.settings?.model &&
          <div className="list-row"><b>Pinned model</b><span>{overview.data.settings.model}</span></div>}
        <div className="list-row"><b>Power</b><span>{overview.data?.settings?.routing === "manual" ? "not in use" : (overview.data?.settings?.power || "–")}</span></div>
        <div className="list-row"><b>Autonomy</b><span>{overview.data?.settings?.autonomy || "–"}</span></div>
        <div className="list-row"><b>Bootstrap</b><span>{overview.data?.bootstrap?.status || "–"}</span></div>
        {/* Depends on a binary north cannot install for you, so the row carries
            what to do about it rather than only that it is missing. */}
        <div className="list-row">
          <b>Browser</b>
          <span title={overview.data?.browser?.detail || ""}>
            {overview.data?.browser?.state || "–"}
            {overview.data?.browser?.state === "unavailable" && (
              <small className="row-hint">{overview.data.browser.detail}</small>
            )}
          </span>
        </div>
        {/* The one model north runs itself. Worth naming: every stored vector is
            stamped with it, so "which embeddings am I on?" decides which memories
            can still be compared with which. */}
        <div className="list-row">
          <div><b>Embeddings</b><small>{overview.data?.embeddings?.local ? "on-device · no data leaves this machine" : overview.data?.embeddings?.provider || "not configured"}</small></div>
          <span className="pool-availability">{overview.data?.embeddings?.model || "–"}</span>
        </div>
      </Panel>
    </div>
  </div>;
}

export function Bootstrap({ embedded = false }: { embedded?: boolean }) {
  const resource = useResource<any>("/web/api/system", 5000); const [message, setMessage] = useState(""); const bootstrap = resource.data?.bootstrap; const completed = new Set((bootstrap?.completed || []).map((item: any) => item.path)); const candidates = bootstrap?.candidates || [];
  const start = async () => { setMessage("Starting bootstrap…"); try { const result = await post<any>("/web/api/bootstrap", { paths: [] }); setMessage(`${result.selected || 0} document(s) queued for bootstrap.`); await resource.reload(); } catch (error) { setMessage(String(error)); } };
  const content = <><div className="bootstrap-heading"><div><div className="eyebrow">Bootstrap coverage</div><h2>Documents North has read</h2><p>Track eligible documents and refresh durable context in one batch.</p></div><button className="primary-button" onClick={start}>Run bootstrap</button></div><div className="metric-cards"><div><span>Status</span><strong>{bootstrap?.status || "–"}</strong></div><div><span>Eligible documents</span><strong>{bootstrap?.candidate_count || 0}</strong></div><div><span>Completed documents</span><strong>{completed.size}</strong></div></div>{message && <div className="notice">{message}</div>}<Panel title="Document coverage" label="Bootstrap sources"><div className="bootstrap-list">{candidates.map((path: string) => <div className="bootstrap-row" key={path}><span><b>{path.split("/").pop()}</b><small>{path}</small></span><Status value={completed.has(path) ? "completed" : "pending"}/></div>)}</div>{!candidates.length && <Empty>No eligible bootstrap documents found.</Empty>}</Panel></>;
  return embedded ? <section className="bootstrap-embedded">{content}</section> : <div className="page">{content}</div>;
}
