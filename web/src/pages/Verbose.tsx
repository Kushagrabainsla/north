import {
  FormEvent,
  ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ChangeEvent, TextareaHTMLAttributes } from "react";
import { api, del, patch, post } from "../api";
import {
  configureDisplayTimezone,
  dateInNorthTimezone,
  Empty,
  ErrorNotice,
  HealthIndicator,
  Loading,
  Markdown,
  PageHeader,
  Panel,
  Status,
  timeAgo,
  weekdayInNorthTimezone,
} from "../components";
import { UI_PREFERENCE_KEYS, usePersistentState, useResource } from "../hooks";
import { hhmm } from "../schedule";
import { useDialog } from "../dialog";
import type {
  Approval,
  Artifact,
  CardField,
  LedgerEntry,
  RoutingDecision,
  RoutingSkip,
} from "../types";

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
  const rows = new Map<
    string,
    { label: string; count: number; detail: string }
  >();
  for (const skip of skips) {
    const key = label(skip);
    const row = rows.get(key) || {
      label: key,
      count: 0,
      detail: skip.detail || "",
    };
    rows.set(key, {
      ...row,
      count: row.count + 1,
      detail: row.detail || skip.detail || "",
    });
  }
  return [...rows.values()].sort((a, b) => b.count - a.count);
}

function rollUp(skips: RoutingSkip[]): ProviderRoll[] {
  const byProvider = new Map<string, RoutingSkip[]>();
  for (const skip of skips)
    byProvider.set(skip.provider, [
      ...(byProvider.get(skip.provider) || []),
      skip,
    ]);
  return [...byProvider]
    .map(([provider, rows]) => ({
      provider,
      tried: tally(
        rows.filter((r) => r.tried),
        (r) => `${r.status_code || "—"} · ${r.reason}`,
      ),
      skipped: tally(
        rows.filter((r) => !r.tried),
        (r) => r.reason,
      ).map(({ label, count }) => ({ label, count })),
      total: rows.length,
    }))
    .sort((a, b) => b.total - a.total);
}

function DecisionCard({ decision }: { decision: RoutingDecision }) {
  const rolls = useMemo(
    () => rollUp(decision.skipped || []),
    [decision.skipped],
  );
  // The stored skip list is capped, so the honest total comes from the row's own
  // counters rather than from counting what happens to have been kept.
  const shown = (decision.skipped || []).length;
  const hidden = Math.max(0, decision.endpoints - shown);
  return (
    <div className="routing-card">
      <header>
        <b>{decision.part}</b>
        {decision.chosen_model ? (
          <span>
            answered by {decision.chosen_model} · {decision.chosen_provider}
          </span>
        ) : (
          <span className="routing-exhausted">no model answered</span>
        )}
        <small>
          {decision.considered} models · {decision.endpoints} endpoints ·{" "}
          {decision.attempted} called
        </small>
      </header>
      {rolls.map((roll) => (
        <div className="routing-provider" key={roll.provider}>
          <b>{roll.provider}</b>
          <div>
            {roll.tried.map((row) => (
              <div className="routing-line" key={`t${row.label}`}>
                <span className="routing-count">{row.count} called</span>
                <span title={row.label}>{row.label}</span>
                {row.detail && <em title={row.detail}>{row.detail}</em>}
              </div>
            ))}
            {roll.skipped.map((row) => (
              <div
                className="routing-line routing-untried"
                key={`s${row.label}`}
              >
                <span className="routing-count">{row.count} skipped</span>
                <span title={row.label}>{row.label}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      {hidden > 0 && (
        <small className="routing-more">
          …and {hidden} more endpoints not listed
        </small>
      )}
    </div>
  );
}

export function RoutingAttempts({ taskId }: { taskId: string }) {
  const resource = useResource<RoutingDecision[]>(
    `/web/api/routing/decisions?task_id=${encodeURIComponent(taskId)}`,
  );
  if (resource.loading) return <Loading />;
  if (resource.error) return <ErrorNotice message={resource.error} />;
  const decisions = resource.data || [];
  if (!decisions.length) return <Empty>No routing record for this task.</Empty>;
  return (
    <div className="routing-detail">
      {decisions.map((d) => (
        <DecisionCard decision={d} key={d.id} />
      ))}
    </div>
  );
}

// The pipeline stages, in the order they run - so a task's artifacts read as the
// story of that run rather than in whatever order the filesystem returned them.
const stageOrder = ["research", "architecture", "implementation", "qa"];
export const stageIcon: Record<string, string> = {
  news: "☼",
  notes: "✎",
  wellness: "♥",
  research: "◇",
  architecture: "▣",
  implementation: "▸",
  qa: "✓",
};

function ArtifactLibrary({ newsOnly = false }: { newsOnly?: boolean }) {
  const resource = useResource<Artifact[]>("/web/api/artifacts", 10000);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const files = (resource.data || []).filter(
    (file) => !newsOnly || file.kind === "news",
  );
  const open = async (file: Artifact) => {
    try {
      setSelected(await api<Artifact>(`/web/api/artifacts/${file.id}`));
    } catch (err) {
      setError(String(err));
    }
  };
  const personal = files.filter((file) => !file.task);
  // One group per task run, newest run first, stages in pipeline order within it.
  const runs = useMemo(() => {
    const grouped = new Map<string, Artifact[]>();
    for (const file of files)
      if (file.task)
        grouped.set(file.task, [...(grouped.get(file.task) || []), file]);
    return [...grouped]
      .map(([task, items]) => ({
        task,
        items: [...items].sort(
          (a, b) =>
            stageOrder.indexOf(a.kind) - stageOrder.indexOf(b.kind) ||
            a.name.localeCompare(b.name),
        ),
        updated: Math.max(...items.map((item) => item.updated_at || 0)),
      }))
      .sort((a, b) => b.updated - a.updated);
  }, [files]);
  const card = (file: Artifact) => (
    <button className="artifact-card" key={file.id} onClick={() => open(file)}>
      <span>{stageIcon[file.kind] || "◇"}</span>
      <div>
        <b>{file.name}</b>
        <small>
          {file.kind} · {file.size ? `${Math.ceil(file.size / 1024)} KB` : ""} ·{" "}
          {timeAgo(file.updated_at)}
        </small>
      </div>
    </button>
  );
  if (resource.loading) return <Loading />;
  return (
    <>
      {(resource.error || error) && (
        <ErrorNotice message={resource.error || error} />
      )}
      {!!personal.length && (
        <div className="artifact-grid">{personal.map(card)}</div>
      )}
      {runs.map((run) => (
        <section className="artifact-run" key={run.task}>
          <h3>
            {run.task}
            <small>{timeAgo(run.updated)}</small>
          </h3>
          <div className="artifact-grid">{run.items.map(card)}</div>
        </section>
      ))}
      {!files.length && (
        <Empty>No files have been generated in this section.</Empty>
      )}
      {selected && (
        <div className="document-view">
          <header>
            <div>
              <span>
                {selected.task
                  ? `${selected.task} · ${selected.kind}`
                  : selected.kind}
              </span>
              <h2>{selected.name}</h2>
            </div>
            <button onClick={() => setSelected(null)}>Close</button>
          </header>
          <Markdown>{selected.content || ""}</Markdown>
        </div>
      )}
    </>
  );
}

export function Artifacts() {
  return (
    <div className="page">
      <PageHeader
        eyebrow="Outputs"
        title="Artifacts"
        subtitle="Every report, briefing, note, plan, and file North has produced."
      />
      <ArtifactLibrary />
    </div>
  );
}

const fieldLabel = (field: CardField) =>
  field.label ||
  field.name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// One filled-in field. Read-only ones render as text so the card reads as work
// to check rather than a form to fill: the point is to see what North put there,
// and only the parts it offered as editable invite typing.
function CardFieldRow({
  field,
  value,
  onChange,
}: {
  field: CardField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const text = value === null || value === undefined ? "" : String(value);
  let control;
  if (!field.editable) {
    control =
      field.type === "link" ? (
        <a
          className="card-field-value"
          href={text}
          target="_blank"
          rel="noreferrer"
        >
          {text}
        </a>
      ) : (
        <div className="card-field-value">
          {field.type === "boolean" ? (value ? "yes" : "no") : text || "—"}
        </div>
      );
  } else if (field.type === "textarea") {
    control = (
      <textarea
        value={text}
        rows={6}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (field.type === "boolean") {
    control = (
      <input
        type="checkbox"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  } else if (field.type === "select") {
    control = (
      <select value={text} onChange={(e) => onChange(e.target.value)}>
        {field.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  } else {
    control = (
      <input
        type={field.type === "number" ? "number" : "text"}
        value={text}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <label className="card-field">
      <span>
        {fieldLabel(field)}
        {field.editable && <em> editable</em>}
      </span>
      {control}
    </label>
  );
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
const REJECTION_REASONS = [
  "Not relevant",
  "Wrong details",
  "Already handled",
  "Bad timing",
  "Not interested",
];

function ApprovalCard({
  card,
  onDecide,
}: {
  card: Approval;
  onDecide: (
    decision: string,
    chosen_option: string,
    values: Record<string, unknown>,
    reason?: string,
  ) => void;
}) {
  const fields = card.fields || [];
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, f.value])),
  );
  const [showContext, setShowContext] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const edited = fields.filter(
    (f) => f.editable && values[f.name] !== f.value,
  ).length;
  // Prepared work is judged against its source, so the source is shown. For a
  // guard-rail the context is incidental and stays behind a toggle - collapsing
  // it there is right, and collapsing it here would make deciding-without-
  // looking the default path, which is the specific thing to make harder.
  const sideBySide = !card.blocking && Boolean(card.context);
  const body = (
    <>
      {card.message && <p>{card.message}</p>}
      {fields.length > 0 && (
        <div className="card-fields">
          {fields.map((field) => (
            <CardFieldRow
              key={field.name}
              field={field}
              value={values[field.name]}
              onChange={(v) =>
                setValues((prev) => ({ ...prev, [field.name]: v }))
              }
            />
          ))}
        </div>
      )}
      {card.context && !sideBySide && (
        <div className="card-context">
          <button
            className="link-button"
            onClick={() => setShowContext(!showContext)}
          >
            {showContext ? "Hide" : "Show"} source
          </button>
          {showContext && <pre>{card.context}</pre>}
        </div>
      )}
    </>
  );
  return (
    <article className="approval-card">
      <div className="approval-type">
        {card.type}
        {!card.blocking && (
          <span className="card-unblocking"> · nothing is waiting on this</span>
        )}
      </div>
      <h2>{card.title}</h2>
      {sideBySide ? (
        <div className="approval-split">
          <div className="approval-work">{body}</div>
          <aside className="approval-source">
            <div className="editor-label">Source</div>
            <pre>{card.context}</pre>
          </aside>
        </div>
      ) : (
        body
      )}
      <small>
        {card.agent} · {timeAgo(card.created_at)}
        {edited > 0 && ` · ${edited} field${edited > 1 ? "s" : ""} edited`}
      </small>
      {/* What approving will actually do. Two cards with identical buttons can
        submit an application and save a draft respectively. */}
      {card.next_step && (
        <p className="approval-consequence">
          Approving will <b>{card.next_step}</b>.
        </p>
      )}
      {/* Asked only for prepared work. A guard-rail rejection is a decision about
        one action in one task, not an example of what should be proposed, so
        interrupting it for a reason would cost a click and teach nothing. */}
      {rejecting ? (
        <div className="reject-reason">
          <div className="editor-label">
            Why? This is what makes the next batch better.
          </div>
          <div className="reason-chips">
            {REJECTION_REASONS.map((chip) => (
              <button
                key={chip}
                className={reason === chip ? "active" : ""}
                onClick={() => setReason(chip)}
              >
                {chip}
              </button>
            ))}
          </div>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="or say why in your own words…"
          />
          <div className="approval-actions">
            <button
              className="danger-button"
              onClick={() => onDecide("rejected", "", values, reason)}
            >
              Reject
            </button>
            <button
              onClick={() => {
                setRejecting(false);
                setReason("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="approval-actions">
          {card.type === "question" ? (
            card.options.map((option) => (
              <button
                key={option}
                onClick={() => onDecide("answered", option, values)}
              >
                {option}
              </button>
            ))
          ) : (
            <>
              <button
                className="primary-button"
                onClick={() => onDecide("approved", "", values)}
              >
                Approve
              </button>
              <button
                className="danger-button"
                onClick={() =>
                  card.source
                    ? setRejecting(true)
                    : onDecide("rejected", "", values)
                }
              >
                Reject
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

export function Approvals() {
  // A slow poll as the floor, with SSE on top. The stream is what makes a new
  // card appear; the poll is what stops a dropped connection turning into a
  // queue that has silently stopped updating.
  const resource = useResource<Approval[]>("/web/api/approvals", 30000);
  const { reload } = resource;
  useEffect(() => {
    const stream = new EventSource("/orchestrator/stream");
    for (const event of [
      "approval_required",
      "question_required",
      "approval_responded",
    ]) {
      stream.addEventListener(event, () => {
        void reload();
      });
    }
    return () => stream.close();
  }, [reload]);

  const decide = async (
    card: Approval,
    decision: string,
    chosen_option = "",
    values: Record<string, unknown> = {},
    reason = "",
  ) => {
    await post("/orchestrator/approval/respond", {
      card_id: card.id,
      decision,
      chosen_option,
      values,
      reason,
    });
    await resource.reload();
  };
  if (resource.loading) return <Loading />;
  const pending = (resource.data || []).filter(
    (card) => card.status === "pending",
  );
  const history = (resource.data || []).filter(
    (card) => card.status !== "pending",
  );

  // Split on whether anything is waiting, not on urgency-as-styling. An agent
  // frozen mid-action and an application that can wait until tonight have
  // different costs of being missed, and one undifferentiated stack hides that.
  // Two pages was considered and rejected: two inboxes means missed items.
  const blocking = pending.filter((card) => card.blocking);
  const prepared = pending.filter((card) => !card.blocking);

  // Deliberately no "approve all". Reject-all would be fine; a bulk approve is
  // the fastest possible review and the worst one, and this page exists for
  // per-item judgement.
  const section = (cards: Approval[]) => (
    <div className="approval-stack">
      {cards.map((card) => (
        <ApprovalCard
          key={card.id}
          card={card}
          onDecide={(d, o, v, r) => decide(card, d, o, v, r)}
        />
      ))}
    </div>
  );

  return (
    <div className="page">
      <PageHeader
        eyebrow="Attention"
        title="Approvals"
        subtitle={
          pending.length
            ? `${pending.length} waiting · ${blocking.length} blocking something`
            : "Questions and consequential actions waiting for your decision."
        }
      />
      {resource.error && <ErrorNotice message={resource.error} />}

      {blocking.length > 0 && (
        <>
          <h2 className="section-title">Needs you now</h2>
          <p className="muted memory-note">
            Something is stopped until you answer.
          </p>
          {section(blocking)}
        </>
      )}

      {prepared.length > 0 && (
        <>
          <h2 className="section-title">When you have a minute</h2>
          <p className="muted memory-note">
            Work north finished and left for you. Nothing is blocked; take the
            time to read it.
          </p>
          {section(prepared)}
        </>
      )}

      {!pending.length && <Empty>Nothing needs your attention.</Empty>}

      <h2 className="section-title">Resolved</h2>
      <div className="table-list">
        {history.map((card) => (
          <div className="table-row" key={card.id}>
            <div className="row-main">
              <b>{card.title}</b>
              <small>
                {card.agent} · {timeAgo(card.created_at)}
              </small>
            </div>
            <Status value={card.status} />
          </div>
        ))}
      </div>
    </div>
  );
}

// Only what the agent-deletion prompt counts: the queued work an agent owns.
interface Job {
  job_id: string;
  agent: string;
}

interface Cron {
  name: string;
  label: string;
  title: string;
  description: string;
  agent: string;
  task: string;
  flow?: string;
  hour: number;
  minute: number;
  interval_minutes?: number | null;
  anchor_epoch?: number | null;
  weekdays: number[];
  cadence: string;
  enabled: boolean;
  tz: string;
  schedule: string;
  next_run_local: string;
  next_run_epoch: number;
  source: string;
  modified: boolean;
}

const docs = ["user.md", "north_stars.md", "judgement_rules.md", "soul.md"];
const docLabels: Record<string, string> = {
  "user.md": "User facts",
  "north_stars.md": "North stars",
  "judgement_rules.md": "Judgement rules",
  "soul.md": "Soul",
};
interface ContextDoc {
  document: string;
  content: string;
}
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

interface Episode {
  id: string;
  task_id: string;
  domain: string;
  outcome: string;
  summary: string;
  timestamp: string;
}
interface ApprovalDecision {
  fingerprint: string;
  agent: string;
  signature: string;
  decision: string;
  count: number;
  updated_at: string;
}

// What north replays instead of asking, once autonomy is turned up. Shown so it
// can be checked before it is trusted, and forgotten one row at a time - a
// decision that cannot be withdrawn is not consent.
function ApprovalMemoryPanel() {
  const resource = useResource<ApprovalDecision[]>(
    "/web/api/memory/approvals",
    10000,
  );
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const forget = async (row: ApprovalDecision) => {
    setBusy(row.fingerprint);
    setError("");
    try {
      await del(
        `/web/api/memory/approvals/${encodeURIComponent(row.fingerprint)}`,
      );
      await resource.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };
  const rows = resource.data || [];
  return (
    <Panel title="Learned decisions" label={`${rows.length} remembered`}>
      {error && <ErrorNotice message={error} />}
      <p className="muted memory-note">
        Every time you approve or reject an action, north records it against a
        fingerprint of that action and replays your decision when a matching one
        comes up. This is what autonomous mode runs on.
      </p>
      {resource.loading ? (
        <Loading />
      ) : rows.length ? (
        rows.map((row) => (
          <div className="memory-row" key={row.fingerprint}>
            <div className="memory-main">
              <b>{row.signature}</b>
              <small>
                {row.agent} ·{" "}
                <span className={`decision decision-${row.decision}`}>
                  {row.decision}
                </span>
                {row.count > 1 && ` · ${row.count}×`} ·{" "}
                {timeAgo(row.updated_at)}
              </small>
            </div>
            <button
              className="ghost-button danger-link"
              disabled={busy === row.fingerprint}
              onClick={() => forget(row)}
            >
              Forget
            </button>
          </div>
        ))
      ) : (
        <Empty>
          Nothing learned yet. Approve or reject an action and it appears here.
        </Empty>
      )}
    </Panel>
  );
}

interface UnattendedRule {
  id: string;
  kind: string;
  pattern: string;
  enabled: boolean;
  source: string;
  note: string;
  fire_count: number;
  last_fired_at: string | null;
}
interface UnattendedRules {
  mode: string;
  active: boolean;
  kinds: string[];
  rules: UnattendedRule[];
}

const KIND_LABELS: Record<string, string> = {
  command: "Commands",
  git: "Git actions",
  device: "Device toggles",
  self_message: "Messages to you",
};

const KIND_HELP: Record<string, string> = {
  command:
    "Run without asking. Matched as a whole command or a prefix; chaining, pipes and redirects are always refused.",
  git: "Local, reversible git only. Push, pull, merge and forced variants are never auto-approved.",
  device:
    "Trivially reversible physical actions, where the undo is another toggle.",
  self_message:
    "Messages addressed to you. A message to anyone else is never auto-approved.",
};

// The safe-action list north runs in `auto` mode without asking. It used to be a
// tuple in a source file: you could not see it, add to it, or take anything out
// of it. approval_memory next door already settled the principle - a decision
// that cannot be withdrawn is not consent - and a hardcoded allowlist fails it.
function SafeActionsPanel() {
  const dialog = useDialog();
  const resource = useResource<UnattendedRules>(
    "/web/api/unattended/rules",
    10000,
  );
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [kind, setKind] = useState("command");
  const [pattern, setPattern] = useState("");

  const run = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError("");
    try {
      await fn();
      await resource.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };
  const toggle = (row: UnattendedRule) =>
    run(row.id, () =>
      patch(`/web/api/unattended/rules/${encodeURIComponent(row.id)}`, {
        enabled: !row.enabled,
      }),
    );
  const remove = async (row: UnattendedRule) => {
    const shipped = row.source === "builtin";
    const ok = await dialog.confirm(
      shipped
        ? "It stays in the list, switched off, so you can restore it later."
        : "This rule is yours, so removing it deletes it.",
      {
        title: shipped
          ? `Disable "${row.pattern}"?`
          : `Delete "${row.pattern}"?`,
        confirmLabel: shipped ? "Disable" : "Delete",
        danger: true,
      },
    );
    if (!ok) return;
    return run(row.id, () =>
      del(`/web/api/unattended/rules/${encodeURIComponent(row.id)}`),
    );
  };
  const add = () => {
    if (!pattern.trim()) return;
    return run("new", async () => {
      await post("/web/api/unattended/rules", {
        kind,
        pattern: pattern.trim(),
      });
      setPattern("");
    });
  };
  const restore = () =>
    run("restore", () => post("/web/api/unattended/rules/restore", {}));

  const data = resource.data;
  const rules = data?.rules || [];
  const kinds = data?.kinds || Object.keys(KIND_LABELS);

  return (
    <Panel
      title="Safe actions"
      label={`${rules.filter((r) => r.enabled).length} active`}
      actions={
        <button
          className="ghost-button"
          disabled={busy === "restore"}
          onClick={restore}
        >
          Restore shipped rules
        </button>
      }
    >
      {error && <ErrorNotice message={error} />}

      {/* A rule only ever fires in `auto`. Listing rules that cannot fire, with
        nothing saying so, is how someone concludes the feature is broken. */}
      {data && !data.active && (
        <div className="notice">
          These rules are inactive: north is in <b>{data.mode}</b> mode and asks
          about everything. They apply in <b>auto</b> mode.
        </div>
      )}

      <p className="muted memory-note">
        What north may do without asking, in auto mode. Two things are never on
        this list, whatever you add: sending something to another person, and
        spending money. A sent email cannot be unsent.
      </p>

      <div className="fact-editor">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k] || k}
            </option>
          ))}
        </select>
        <input
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
          placeholder={kind === "command" ? "e.g. make lint" : "e.g. fetch"}
          onKeyDown={(e) => {
            if (e.key === "Enter") add();
          }}
        />
        <button
          className="ghost-button"
          disabled={busy === "new"}
          onClick={add}
        >
          Add rule
        </button>
      </div>

      {resource.loading ? (
        <Loading />
      ) : (
        kinds.map((k) => {
          const forKind = rules.filter((r) => r.kind === k);
          if (!forKind.length) return null;
          return (
            <div className="rule-group" key={k}>
              <div className="editor-label">{KIND_LABELS[k] || k}</div>
              <p className="muted memory-note">{KIND_HELP[k]}</p>
              {forKind.map((row) => (
                <div
                  className={`memory-row ${row.enabled ? "" : "rule-disabled"}`}
                  key={row.id}
                >
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
                    <button
                      disabled={busy === row.id}
                      onClick={() => toggle(row)}
                    >
                      {row.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      className="danger-link"
                      disabled={busy === row.id}
                      onClick={() => remove(row)}
                    >
                      {row.source === "builtin" ? "Remove" : "Delete"}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          );
        })
      )}
      {!resource.loading && !rules.length && (
        <Empty>No safe actions configured.</Empty>
      )}
    </Panel>
  );
}

// A record of what happened, so read-only: the honest way to change an episode
// is to do the thing differently, not to edit the note.
function EpisodesPanel() {
  const resource = useResource<Episode[]>("/web/api/memory/episodes", 10000);
  const rows = resource.data || [];
  return (
    <Panel title="Episodes" label={`${rows.length} remembered`}>
      <p className="muted memory-note">
        What north remembers of past tasks, used to recognise a situation it has
        been in before.
      </p>
      {resource.loading ? (
        <Loading />
      ) : rows.length ? (
        rows.map((row) => (
          <div className="memory-row" key={row.id}>
            <div className="memory-main">
              <b>{row.summary}</b>
              <small>
                {row.domain} · {row.task_id} · {timeAgo(row.timestamp)}
              </small>
            </div>
            <Status value={row.outcome} />
          </div>
        ))
      ) : (
        <Empty>No episodes recorded yet.</Empty>
      )}
    </Panel>
  );
}

export function Memory() {
  const dialog = useDialog();
  const [tab, setTab] = usePersistentState<MemoryTab>(
    UI_PREFERENCE_KEYS.memoryTab,
    "documents",
    (value) => MEMORY_TABS.some(([name]) => name === value),
  );
  const [doc, setDoc] = usePersistentState(
    UI_PREFERENCE_KEYS.memoryDocument,
    docs[0],
    (value) => typeof value === "string" && docs.includes(value),
  );
  const resource = useResource<ContextDoc>(`/orchestrator/context/${doc}`);
  const facts = useResource<any[]>("/web/api/memory/facts", 10000);
  const [draft, setDraft] = useState<string | null>(null);
  const [factDraft, setFactDraft] = useState("");
  const [editingFact, setEditingFact] = useState<string | null>(null);
  const isUserFacts = doc === "user.md";
  const content = isUserFacts ? "" : (draft ?? resource.data?.content ?? "");
  const save = async () => {
    await api(`/orchestrator/context/${doc}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    setDraft(null);
    await resource.reload();
  };
  const removeDocument = async () => {
    if (
      !(await dialog.confirm(
        `This puts ${docLabels[doc]} back to what north ships with. Anything written here is lost.`,
        {
          title: `Reset ${docLabels[doc]}?`,
          confirmLabel: "Reset",
          danger: true,
        },
      ))
    )
      return;
    await api(`/orchestrator/context/${doc}`, { method: "DELETE" });
    setDraft(null);
    await resource.reload();
  };
  const saveFact = async () => {
    if (!factDraft.trim()) return;
    if (editingFact)
      await api(`/web/api/memory/facts/${editingFact}`, {
        method: "PATCH",
        body: JSON.stringify({ content: factDraft, category: "user" }),
      });
    else
      await post("/web/api/memory/facts", {
        content: factDraft,
        category: "user",
      });
    setFactDraft("");
    setEditingFact(null);
    await facts.reload();
  };
  const removeFact = async (id: string) => {
    if (
      !(await dialog.confirm(
        "north will stop using it, and stop recalling it for future tasks.",
        { title: "Forget this fact?", confirmLabel: "Forget", danger: true },
      ))
    )
      return;
    await api(`/web/api/memory/facts/${id}`, { method: "DELETE" });
    await facts.reload();
  };
  const onDocuments = tab === "documents";
  return (
    <div className="page">
      <PageHeader
        eyebrow="Knowledge"
        title="Memory"
        subtitle="Everything north has learned about you, and from you."
        actions={
          onDocuments && !isUserFacts ? (
            <>
              <button onClick={removeDocument}>Reset document</button>
              <button
                className="primary-button"
                disabled={draft === null}
                onClick={save}
              >
                Save document
              </button>
            </>
          ) : null
        }
      />
      <div className="segmented memory-tabs">
        {MEMORY_TABS.map(([value, label]) => (
          <button
            className={tab === value ? "active" : ""}
            key={value}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {onDocuments && (
        <div className="memory-layout">
          <aside>
            {docs.map((name) => (
              <button
                className={doc === name ? "active" : ""}
                key={name}
                onClick={() => {
                  setDoc(name);
                  setDraft(null);
                }}
              >
                {docLabels[name]}
              </button>
            ))}
          </aside>
          <div className="memory-editor-grid">
            <section>
              <div className="editor-label">
                {isUserFacts ? "User facts" : "Markdown source"}
              </div>
              {isUserFacts ? (
                <div className="document-help">
                  User details are managed as individual facts on the Facts tab.
                </div>
              ) : resource.loading ? (
                <Loading />
              ) : (
                <textarea
                  className="document-editor"
                  value={content}
                  onChange={(e) => setDraft(e.target.value)}
                />
              )}
            </section>
            <section
              className={`memory-preview ${isUserFacts ? "user-facts-preview" : ""}`}
            >
              <div className="editor-label">Rendered preview</div>
              {isUserFacts ? (
                <div className="document-help">
                  Your durable user details live on the Facts tab.
                </div>
              ) : (
                <Markdown>{content || "Nothing written yet."}</Markdown>
              )}
            </section>
          </div>
        </div>
      )}

      {tab === "facts" && (
        <section className="facts-panel panel">
          <header>
            <div>
              <span>Durable context</span>
              <h2>Facts</h2>
            </div>
            <span>{facts.data?.length || 0} stored</span>
          </header>
          <div className="panel-content">
            <div className="fact-editor">
              <input
                value={factDraft}
                onChange={(e) => setFactDraft(e.target.value)}
                placeholder="Add one atomic fact…"
              />
              <button className="ghost-button" onClick={saveFact}>
                {editingFact ? "Update" : "Add fact"}
              </button>
              {editingFact && (
                <button
                  className="ghost-button"
                  onClick={() => {
                    setEditingFact(null);
                    setFactDraft("");
                  }}
                >
                  Cancel
                </button>
              )}
            </div>
            <div className="fact-list">
              {facts.loading ? (
                <Loading />
              ) : (
                (facts.data || []).map((fact) => (
                  <article className="fact-row" key={fact.id}>
                    <div>
                      <b>{fact.content}</b>
                      <small>
                        {fact.category || "general"} · updated{" "}
                        {timeAgo(fact.updated_at)}
                      </small>
                    </div>
                    <div className="fact-actions">
                      <span>
                        {fact.confidence != null
                          ? `${Math.round(Number(fact.confidence) * 100)}%`
                          : "Active"}
                      </span>
                      <button
                        onClick={() => {
                          setEditingFact(fact.id);
                          setFactDraft(fact.content);
                        }}
                      >
                        Edit
                      </button>
                      <button onClick={() => removeFact(fact.id)}>
                        Delete
                      </button>
                    </div>
                  </article>
                ))
              )}
              {!facts.loading && !facts.data?.length && (
                <Empty>No durable facts have been captured yet.</Empty>
              )}
            </div>
          </div>
        </section>
      )}

      {tab === "episodes" && <EpisodesPanel />}
      {tab === "approvals" && <ApprovalMemoryPanel />}
      {tab === "rules" && <SafeActionsPanel />}
      {onDocuments && <Bootstrap embedded />}
    </div>
  );
}

interface Agent {
  name: string;
  domain: string;
  model_pool: string;
  accepts: string[];
  source: string;
  deletable: boolean;
}
interface Confidence {
  agent: string;
  tool: string;
  confidence: number;
}
export function agentDeletionPrompt(
  agentName: string,
  scheduleTitles: string[],
  pendingJobCount: number,
): string {
  const scheduleList = scheduleTitles.length
    ? `\n\nSchedules also deleted:\n${scheduleTitles.map((title) => `• ${title}`).join("\n")}`
    : "";
  const jobNotice = pendingJobCount
    ? `\n\n${pendingJobCount} pending job${pendingJobCount === 1 ? "" : "s"} will also be cancelled.`
    : "";
  return `Delete personal agent '${agentName}'?${scheduleList}${jobNotice}`;
}

export function Agents() {
  const dialog = useDialog();
  const agents = useResource<Agent[]>("/orchestrator/agents");
  const confidence = useResource<Confidence[]>(
    "/orchestrator/tools/confidence",
  );
  const schedules = useResource<Cron[]>("/orchestrator/cron");
  const pendingJobs = useResource<Job[]>(
    "/orchestrator/jobs?status=pending&limit=1000",
  );
  const [message, setMessage] = useState("");
  const cascadeReady =
    !schedules.loading &&
    !pendingJobs.loading &&
    !schedules.error &&
    !pendingJobs.error;
  const remove = async (agent: Agent) => {
    const linkedSchedules = (schedules.data || []).filter(
      (item) => item.agent === agent.name,
    );
    const linkedJobs = (pendingJobs.data || []).filter(
      (item) => item.agent === agent.name,
    );
    if (
      !agent.deletable ||
      !cascadeReady ||
      !(await dialog.confirm(
        agentDeletionPrompt(
          agent.name,
          linkedSchedules.map((item) => item.title),
          linkedJobs.length,
        ),
        {
          title: "Delete personal agent?",
          confirmLabel: "Delete agent",
          danger: true,
        },
      ))
    )
      return;
    try {
      await del(`/orchestrator/agents/${encodeURIComponent(agent.name)}`);
      setMessage(`Deleted personal agent '${agent.name}' and its future work.`);
      await Promise.all([
        agents.reload(),
        schedules.reload(),
        pendingJobs.reload(),
      ]);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="page">
      <PageHeader
        eyebrow="Capabilities"
        title="Agents"
        subtitle="North's specialist team and the tools they trust."
      />
      {agents.error && <ErrorNotice message={agents.error} />}{" "}
      {schedules.error && <ErrorNotice message={schedules.error} />}{" "}
      {pendingJobs.error && <ErrorNotice message={pendingJobs.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      <div className="agent-grid">
        {agents.data?.map((agent) => (
          <article key={agent.name}>
            <div className="agent-avatar">
              {agent.name.slice(0, 1).toUpperCase()}
            </div>
            <h2>{agent.name}</h2>
            <p>
              {agent.domain} · {agent.model_pool} · {agent.source}
            </p>
            <div className="tag-list">
              {agent.accepts.slice(0, 5).map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
            <h3>Tool confidence</h3>
            {confidence.data
              ?.filter((item) => item.agent === agent.name)
              .slice(0, 4)
              .map((item) => (
                <div className="confidence" key={item.tool}>
                  <span>{item.tool}</span>
                  <i>
                    <b style={{ width: `${item.confidence * 100}%` }} />
                  </i>
                </div>
              ))}
            {agent.deletable && (
              <button
                className="danger-button"
                disabled={!cascadeReady}
                onClick={() => void remove(agent)}
              >
                Delete personal agent
              </button>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}

export function CapabilityModal({
  open,
  title,
  eyebrow,
  actions,
  className = "",
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  eyebrow: string;
  actions?: ReactNode;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      className={`capability-modal ${className}`}
      ref={ref}
      aria-label={`${title} ${eyebrow}`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="capability-modal-shell">
        <header className="capability-modal-header">
          <div>
            <span>{eyebrow}</span>
            <h2>{title}</h2>
          </div>
          <div className="capability-modal-actions">
            <button className="ghost-button" onClick={onClose}>
              Close
            </button>
            {actions}
          </div>
        </header>
        <div className="capability-modal-content">{children}</div>
      </div>
    </dialog>
  );
}

export interface SkillExecution {
  agent: string;
  tools: string[];
  approval: "never" | "on_mutation" | "always";
  inputs: {
    properties?: Record<string, { type?: string; enum?: unknown[] }>;
    required?: string[];
  };
  outputs: {
    properties?: Record<string, { type?: string; enum?: unknown[] }>;
    required?: string[];
  };
  success_criteria: string[];
}
export interface SkillSummary {
  name: string;
  description: string;
  source: string;
  version: string;
  status: string;
  domains: string[];
  execution: SkillExecution | null;
}
interface SkillDetail {
  name: string;
  content: string;
  source: string;
  execution: SkillExecution | null;
}
interface SkillCreateDraft {
  name: string;
  description: string;
  domains: string;
  instructions: string;
  executor: string;
  tools: string[];
  approval: "never" | "on_mutation" | "always";
  inputs: string;
  outputs: string;
  successCriteria: string;
}

const emptySkill = (): SkillCreateDraft => ({
  name: "",
  description: "",
  domains: "general",
  instructions: "",
  executor: "general",
  tools: [],
  approval: "never",
  inputs: "",
  outputs: "result",
  successCriteria:
    "The requested procedure completed and returned verifiable evidence.",
});
const fieldSchema = (value: string) => {
  const names = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    type: "object",
    properties: Object.fromEntries(
      names.map((name) => [name, { type: "string" }]),
    ),
    required: names,
    additionalProperties: true,
  };
};

export function Skills() {
  const dialog = useDialog();
  const skills = useResource<SkillSummary[]>("/web/api/skills", 10000);
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const agents = useResource<Agent[]>("/orchestrator/agents", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [draft, setDraft] = useState<SkillCreateDraft>(emptySkill());
  const [message, setMessage] = useState("");
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setDraft(emptySkill());
    setMessage("");
    setCreating(true);
  };
  const open = async (name: string) => {
    setSelected(name);
    setCreating(false);
    setDetailLoading(true);
    setContent("");
    setMessage("");
    try {
      const detail = await api<SkillDetail>(
        `/web/api/skills/${encodeURIComponent(name)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setDetailLoading(false);
    }
  };
  const selectedSkill = skills.data?.find((skill) => skill.name === selected);
  const create = async () => {
    if (
      !draft.name.trim() ||
      !draft.description.trim() ||
      !draft.instructions.trim()
    )
      return;
    try {
      const created = await post<SkillDetail>("/web/api/skills", {
        name: draft.name.trim(),
        description: draft.description.trim(),
        instructions: draft.instructions.trim(),
        domains: draft.domains
          .split(",")
          .map((domain) => domain.trim())
          .filter(Boolean),
        executor: draft.executor,
        tools: draft.tools,
        approval: draft.approval,
        inputs: fieldSchema(draft.inputs),
        outputs: fieldSchema(draft.outputs),
        success_criteria: draft.successCriteria
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
      });
      setDraft(emptySkill());
      await skills.reload();
      await open(created.name);
      setMessage(
        "Skill candidate created. Validate its selection before activation.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/skills/${selected}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage("Skill saved as a candidate. Validate it before activation.");
      await skills.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedSkill?.source !== "learned" ||
      !(await dialog.confirm(
        `Delete learned skill '${selected}'? This cannot be undone.`,
        { title: "Delete skill?", confirmLabel: "Delete skill", danger: true },
      ))
    )
      return;
    try {
      await del(`/web/api/skills/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage("Learned skill deleted.");
      await skills.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const canCreate =
    draft.name.trim() &&
    draft.description.trim() &&
    draft.instructions.trim() &&
    draft.executor &&
    draft.successCriteria.trim();
  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Procedures"
        title="Skills"
        subtitle="Inspect and edit the playbooks North injects into specialist work."
        actions={
          <button className="primary-button" onClick={startCreating}>
            + New skill
          </button>
        }
      />
      {skills.error && <ErrorNotice message={skills.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      {creating && (
        <section className="capability-create-panel">
          <header>
            <div>
              <span>New procedure</span>
              <h2>Create skill</h2>
              <p>
                Define the procedure and its executable contract once. Flows
                will depend only on this skill.
              </p>
            </div>
            <button className="ghost-button" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </header>
          <div className="tool-create skill-create">
            <div className="skill-create-grid">
              <label>
                Name
                <input
                  autoFocus
                  value={draft.name}
                  placeholder="review-job-match"
                  onChange={(event) =>
                    setDraft({ ...draft, name: event.target.value })
                  }
                />
              </label>
              <label>
                Domains
                <input
                  value={draft.domains}
                  placeholder="general, research"
                  onChange={(event) =>
                    setDraft({ ...draft, domains: event.target.value })
                  }
                />
              </label>
              <label className="skill-create-wide">
                Description
                <input
                  value={draft.description}
                  placeholder="Use when North should review a job match."
                  onChange={(event) =>
                    setDraft({ ...draft, description: event.target.value })
                  }
                />
              </label>
              <label>
                Executor
                <select
                  value={draft.executor}
                  onChange={(event) =>
                    setDraft({ ...draft, executor: event.target.value })
                  }
                >
                  {(agents.data || []).map((agent) => (
                    <option key={agent.name} value={agent.name}>
                      {agent.name} · {agent.domain}
                    </option>
                  ))}
                </select>
                <small>The agent this skill always uses.</small>
              </label>
              <label>
                Minimum approval
                <select
                  value={draft.approval}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      approval: event.target
                        .value as SkillCreateDraft["approval"],
                    })
                  }
                >
                  <option value="never">No mutations</option>
                  <option value="on_mutation">Ask before each mutation</option>
                  <option value="always">Approve the whole step first</option>
                </select>
              </label>
              <label className="skill-create-wide">
                Allowed tools
                <select
                  value=""
                  onChange={(event) => {
                    const name = event.target.value;
                    if (name && !draft.tools.includes(name))
                      setDraft({ ...draft, tools: [...draft.tools, name] });
                  }}
                >
                  <option value="">Add an atomic tool</option>
                  {(tools.data || [])
                    .filter(
                      (tool) =>
                        tool.status === "active" &&
                        !draft.tools.includes(tool.name),
                    )
                    .map((tool) => (
                      <option key={tool.name} value={tool.name}>
                        {tool.name}
                        {tool.mutating ? " · mutating" : ""}
                      </option>
                    ))}
                </select>
                <div className="contract-chips">
                  {draft.tools.length ? (
                    draft.tools.map((name) => (
                      <button
                        type="button"
                        key={name}
                        onClick={() =>
                          setDraft({
                            ...draft,
                            tools: draft.tools.filter((item) => item !== name),
                          })
                        }
                      >
                        {name} ×
                      </button>
                    ))
                  ) : (
                    <small>No tools. This skill can reason only.</small>
                  )}
                </div>
              </label>
              <label>
                Required input fields
                <input
                  value={draft.inputs}
                  placeholder="resume, job_url"
                  onChange={(event) =>
                    setDraft({ ...draft, inputs: event.target.value })
                  }
                />
                <small>Comma-separated names.</small>
              </label>
              <label>
                Required output fields
                <input
                  value={draft.outputs}
                  placeholder="result, evidence"
                  onChange={(event) =>
                    setDraft({ ...draft, outputs: event.target.value })
                  }
                />
                <small>Comma-separated names.</small>
              </label>
              <label className="skill-create-wide">
                Success criteria
                <textarea
                  rows={3}
                  value={draft.successCriteria}
                  placeholder="One verifiable condition per line."
                  onChange={(event) =>
                    setDraft({ ...draft, successCriteria: event.target.value })
                  }
                />
              </label>
              <label className="skill-create-wide">
                Markdown instructions
                <textarea
                  rows={10}
                  value={draft.instructions}
                  placeholder="Write the ordered procedure North should follow."
                  onChange={(event) =>
                    setDraft({ ...draft, instructions: event.target.value })
                  }
                />
              </label>
            </div>
            <div className="tool-create-footer">
              <small>
                Use a lowercase, hyphenated name. Descriptions start with “Use
                when”.
              </small>
              <button
                className="primary-button"
                disabled={!canCreate}
                onClick={() => void create()}
              >
                Create skill
              </button>
            </div>
          </div>
        </section>
      )}
      <section className="capability-registry">
        <div className="capability-registry-head">
          <span>Skill</span>
          <span>Description</span>
          <span>Definition</span>
          <span>Status</span>
          <span />
        </div>
        <div className="capability-registry-list">
          {skills.loading ? (
            <Loading />
          ) : (
            (skills.data || []).map((skill) => (
              <button
                className="capability-registry-row"
                key={skill.name}
                aria-haspopup="dialog"
                onClick={() => void open(skill.name)}
              >
                <span className="capability-registry-name">
                  <b>{skill.name}</b>
                  <small>{skill.source}</small>
                </span>
                <span className="capability-registry-description">
                  {skill.description}
                </span>
                <span className="capability-registry-meta">
                  {skill.execution
                    ? `${skill.execution.agent} · ${skill.execution.tools.length} tools`
                    : "advisory only"}
                </span>
                <Status value={skill.status} />
                <span className="capability-registry-open">Open</span>
              </button>
            ))
          )}
          {!skills.loading && !skills.data?.length && (
            <Empty>No skills have been created yet.</Empty>
          )}
        </div>
      </section>
      <CapabilityModal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Skill"}
        eyebrow={selectedSkill?.source || "Procedure"}
        actions={
          selected ? (
            <>
              <button
                className="primary-button"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save skill
              </button>
              {selectedSkill?.source === "learned" && (
                <button className="danger-button" onClick={() => void remove()}>
                  Delete skill
                </button>
              )}
            </>
          ) : undefined
        }
      >
        {detailLoading ? (
          <Loading />
        ) : (
          <section className="skill-preview-layout">
            <div className="skill-editor-pane">
              <div className="editor-label">
                <span>{selected}/SKILL.md</span>
                <span>
                  {selectedSkill?.source === "builtin"
                    ? "Built-in baseline · editable override"
                    : "Markdown instructions · editable"}
                </span>
              </div>
              <ContentTextarea
                aria-label={`${selected} Markdown skill instructions`}
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            </div>
            <aside className="skill-preview-pane">
              <div className="editor-label">Rendered Markdown</div>
              <div className="skill-preview-content">
                <Markdown>{content || "Nothing written yet."}</Markdown>
              </div>
            </aside>
          </section>
        )}
      </CapabilityModal>
    </div>
  );
}

function ContentTextarea({
  value,
  onChange,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  value: string;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const resize = () => {
    const element = ref.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${element.scrollHeight}px`;
  };
  useEffect(() => {
    resize();
  }, [value]);
  return (
    <textarea
      {...props}
      ref={ref}
      value={value}
      onChange={(event) => {
        onChange(event);
        requestAnimationFrame(resize);
      }}
    />
  );
}

interface ToolSummary {
  name: string;
  description: string;
  source: string;
  status: string;
  mutating: boolean;
}
interface ToolDetail extends ToolSummary {
  content: string;
}

export function Tools() {
  const dialog = useDialog();
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState("");
  const selectedTool = tools.data?.find((tool) => tool.name === selected);
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setName("");
    setDescription("");
    setMessage("");
    setCreating(true);
  };
  const open = async (toolName: string) => {
    setSelected(toolName);
    setCreating(false);
    setDetailLoading(true);
    setContent("");
    setMessage("");
    try {
      const detail = await api<ToolDetail>(
        `/web/api/tools/${encodeURIComponent(toolName)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setDetailLoading(false);
    }
  };
  const create = async () => {
    if (!name.trim() || !description.trim()) return;
    try {
      const created = await post<ToolSummary>("/web/api/tools", {
        name: name.trim(),
        description: description.trim(),
      });
      setName("");
      setDescription("");
      await tools.reload();
      await open(created.name);
      setMessage(
        "Tool candidate created. Implement and test it before activation.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/tools/${encodeURIComponent(selected)}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage(
        "Tool saved as a candidate. Validate and test it before activation.",
      );
      await tools.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedTool?.source !== "learned" ||
      !(await dialog.confirm(
        `Delete learned tool '${selected}'? This cannot be undone.`,
        { title: "Delete tool?", confirmLabel: "Delete tool", danger: true },
      ))
    )
      return;
    try {
      await del(`/web/api/tools/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage("Learned tool deleted.");
      await tools.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Capabilities"
        title="Tools"
        subtitle="Inspect the atomic operations North's skills can call, and manage your learned tools."
        actions={
          <button className="primary-button" onClick={startCreating}>
            + New tool
          </button>
        }
      />
      {tools.error && <ErrorNotice message={tools.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      {creating && (
        <section className="capability-create-panel">
          <header>
            <div>
              <span>New capability</span>
              <h2>Create tool</h2>
              <p>Tools are atomic Python operations called by skills.</p>
            </div>
            <button className="ghost-button" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </header>
          <div className="tool-create">
            <div className="tool-create-grid">
              <label>
                Name
                <input
                  autoFocus
                  value={name}
                  placeholder="search_jobs"
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label>
                Description
                <input
                  value={description}
                  placeholder="What should this operation do?"
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </div>
            <div className="tool-create-footer">
              <small>
                North creates a non-runnable candidate stub. Activation requires
                a successful test.
              </small>
              <button
                className="primary-button"
                disabled={!name.trim() || !description.trim()}
                onClick={() => void create()}
              >
                Create tool
              </button>
            </div>
          </div>
        </section>
      )}
      <section className="capability-registry">
        <div className="capability-registry-head">
          <span>Tool</span>
          <span>Description</span>
          <span>Operation</span>
          <span>Status</span>
          <span />
        </div>
        <div className="capability-registry-list">
          {tools.loading ? (
            <Loading />
          ) : (
            (tools.data || []).map((tool) => (
              <button
                className="capability-registry-row"
                key={tool.name}
                aria-haspopup="dialog"
                onClick={() => void open(tool.name)}
              >
                <span className="capability-registry-name">
                  <b>{tool.name}</b>
                  <small>{tool.source}</small>
                </span>
                <span className="capability-registry-description">
                  {tool.description}
                </span>
                <span className="capability-registry-meta">
                  {tool.mutating ? "Mutating" : "Read-only"}
                </span>
                <Status value={tool.status} />
                <span className="capability-registry-open">Open</span>
              </button>
            ))
          )}
          {!tools.loading && !tools.data?.length && (
            <Empty>No tools are registered yet.</Empty>
          )}
        </div>
      </section>
      <CapabilityModal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Tool"}
        eyebrow={selectedTool?.source || "Capability"}
        actions={
          selected ? (
            <>
              <button
                className="primary-button"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save tool
              </button>
              {selectedTool?.source === "learned" && (
                <button className="danger-button" onClick={() => void remove()}>
                  Delete tool
                </button>
              )}
            </>
          ) : undefined
        }
      >
        {detailLoading ? (
          <Loading />
        ) : (
          <section className="tool-preview-layout">
            <div className="skill-editor-pane tool-editor">
              <div className="editor-label">
                <span>{selected}.py</span>
                <span>Python implementation · editable</span>
              </div>
              <ContentTextarea
                aria-label={`${selected} Python implementation`}
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            </div>
            <aside className="tool-preview-pane">
              <div className="editor-label">Python preview</div>
              <pre>
                <code>{content || "# Nothing implemented yet."}</code>
              </pre>
            </aside>
          </section>
        )}
      </CapabilityModal>
    </div>
  );
}

export function Insights() {
  const metrics = useResource<Record<string, any>>(
    "/orchestrator/metrics?days=30",
    15000,
  );
  const costs = useResource<Record<string, any>>(
    "/orchestrator/inference/costs?period=month",
    15000,
  );
  const models = useResource<Record<string, any>>(
    "/orchestrator/inference/models",
    15000,
  );
  return (
    <div className="page">
      <PageHeader
        eyebrow="Performance"
        title="Insights"
        subtitle="Usage, cost, reliability, and model availability."
      />
      <div className="metric-cards">
        <div>
          <span>Tasks · 30 days</span>
          <strong>{metrics.data?.total_tasks || 0}</strong>
        </div>
        <div>
          <span>Input tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_in || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Output tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_out || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Model cost</span>
          <strong>${Number(costs.data?.total_cost_usd || 0).toFixed(4)}</strong>
        </div>
      </div>
      <div className="two-column">
        <Panel title="Cost by model">
          {Object.entries(costs.data?.by_model || {}).map(([name, value]) => (
            <div className="list-row" key={name}>
              <b>{name}</b>
              <span>${Number(value).toFixed(4)}</span>
            </div>
          ))}
        </Panel>
        <Panel title="Model pools">
          {Object.entries(models.data || {}).map(
            ([name, pool]: [string, any]) => (
              <div className="list-row" key={name}>
                <div>
                  <b>{name}</b>
                  <small>{pool.models?.length || 0} models available</small>
                </div>
                <span className="pool-availability">Available</span>
              </div>
            ),
          )}
        </Panel>
      </div>
    </div>
  );
}

interface ChainModel {
  model: string;
  score: number;
  price: number | null;
  providers: string[];
  available: boolean;
  skipped_because: string;
}
/** One part of a task, and the models north would try for it, in order. */
interface PartChain {
  part: string;
  requires: string[];
  order_by: string;
  min_context: number;
  eligible: number;
  models: ChainModel[];
}

interface SettingsData {
  power: string;
  autonomy: string;
  routing: string;
  model: string;
  timezone: string;
  timezone_options: string[];
  local_time: string;
}
interface ProviderModels {
  provider: string;
  models: string[];
}

// Which model answers, when the user is choosing it rather than north. Provider
// first, then that provider's models: a flat list of several hundred ids is not
// a choice anyone can make, and the provider is the half people know.
function ModelPicker({
  value,
  onPick,
  busy,
}: {
  value: string;
  onPick: (spec: string) => void;
  busy: boolean;
}) {
  const catalog = useResource<ProviderModels[]>(
    "/orchestrator/inference/catalog",
  );
  const providers = catalog.data || [];
  // A stored pin is "provider:model_id", but only the provider half is a fixed
  // vocabulary - a model id may itself contain a colon, so it is split once.
  const [chosenProvider, chosenModel] = (() => {
    const at = value.indexOf(":");
    return at > 0 ? [value.slice(0, at), value.slice(at + 1)] : ["", value];
  })();
  const provider = chosenProvider || providers[0]?.provider || "";
  const models =
    providers.find((row) => row.provider === provider)?.models || [];

  if (catalog.loading) return <Loading />;
  if (catalog.error)
    return (
      <ErrorNotice
        message={`Could not read the model catalog: ${catalog.error}`}
      />
    );
  if (!providers.length)
    return (
      <Empty>
        No models are reachable. Check your provider keys under System.
      </Empty>
    );
  return (
    <div className="model-picker">
      <label>
        Provider
        <select
          value={provider}
          disabled={busy}
          onChange={(event) =>
            onPick(
              `${event.target.value}:${(providers.find((r) => r.provider === event.target.value)?.models || [])[0] || ""}`,
            )
          }
        >
          {providers.map((row) => (
            <option value={row.provider} key={row.provider}>
              {row.provider}
            </option>
          ))}
        </select>
      </label>
      <label>
        Model
        <select
          value={chosenModel}
          disabled={busy || !models.length}
          onChange={(event) => onPick(`${provider}:${event.target.value}`)}
        >
          {(models.includes(chosenModel) || !chosenModel
            ? models
            : [chosenModel, ...models]
          ).map((id) => (
            <option value={id} key={id}>
              {id}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = async (body: Partial<SettingsData>) => {
    setBusy(true);
    setError("");
    try {
      const saved = await post<SettingsData>("/orchestrator/settings", body);
      configureDisplayTimezone(saved.timezone);
      await resource.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const routing = resource.data?.routing || "auto";
  const manual = routing === "manual";
  // Manual routing needs a model, and the server refuses the switch without one.
  // So choosing "manual" with nothing stored opens the picker and waits: the
  // switch is made by picking a model, which is the decision anyway.
  const [choosing, setChoosing] = useState(false);
  const pickManual = () =>
    resource.data?.model ? update({ routing: "manual" }) : setChoosing(true);
  return (
    <div className="page">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        subtitle="Control how North balances capability, cost, autonomy, and readability."
      />
      {error && <ErrorNotice message={error} />}
      <div className="settings-grid">
        <Panel title="Model routing" label="Who picks">
          <div className="segmented">
            <button
              className={!manual && !choosing ? "active" : ""}
              disabled={busy}
              onClick={() => {
                setChoosing(false);
                update({ routing: "auto" });
              }}
            >
              auto
            </button>
            <button
              className={manual || choosing ? "active" : ""}
              disabled={busy}
              onClick={pickManual}
            >
              manual
            </button>
          </div>
          <p className="muted">
            {manual
              ? "One model answers everything. North's ranking is skipped, and a model it cannot reach fails the call rather than falling back."
              : choosing
                ? "Pick the model that should answer everything."
                : "North ranks every model it can reach against what each part of a task needs, and calls them in that order."}
          </p>
          {(manual || choosing) && (
            <ModelPicker
              value={resource.data?.model || ""}
              busy={busy}
              onPick={(spec) => {
                setChoosing(false);
                update({ routing: "manual", model: spec });
              }}
            />
          )}
        </Panel>
        {/* Ordering a chain of one is meaningless, so under manual the dial is
          shown switched off rather than left looking live. */}
        <Panel
          title="Power"
          label={manual ? "not in use" : "Model strategy"}
          className={manual ? "panel-inert" : ""}
        >
          <div className="segmented">
            {["eco", "cruise", "sport"].map((value) => (
              <button
                className={resource.data?.power === value ? "active" : ""}
                disabled={busy || manual}
                onClick={() => update({ power: value })}
                key={value}
              >
                {value}
              </button>
            ))}
          </div>
          <p className="muted">
            {manual
              ? "Nothing to order while one model is pinned. Switch routing to auto to use this."
              : "Choose how aggressively North selects capable models."}
          </p>
        </Panel>
        <Panel title="Autonomy" label="Approval behavior">
          <div className="segmented">
            {["interactive", "auto", "autonomous"].map((value) => (
              <button
                className={resource.data?.autonomy === value ? "active" : ""}
                disabled={busy}
                onClick={() => update({ autonomy: value })}
                key={value}
              >
                {value}
              </button>
            ))}
          </div>
          <p className="muted">
            Consequential and destructive actions remain governed by North's
            safety policy.
          </p>
        </Panel>
        <Panel title="Time zone" label="Dates and schedules">
          {resource.loading ? (
            <Loading />
          ) : (
            <label className="timezone-picker">
              North uses
              <select
                value={resource.data?.timezone || "UTC"}
                disabled={busy}
                onChange={(event) => update({ timezone: event.target.value })}
              >
                {(
                  resource.data?.timezone_options || [
                    resource.data?.timezone || "UTC",
                  ]
                ).map((timezone) => (
                  <option value={timezone} key={timezone}>
                    {timezone.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
          )}
          <p className="muted">
            Current North time: {resource.data?.local_time || "–"}. New
            schedules and times without an explicit zone use this setting.
          </p>
        </Panel>
      </div>
    </div>
  );
}

interface ProviderAuthState {
  provider_id: string;
  state:
    | "starting"
    | "pending"
    | "connected"
    | "disconnected"
    | "cancelled"
    | "error";
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
  const costs = useResource<any>(
    "/orchestrator/inference/costs?period=month",
    15000,
  );
  const chains = useResource<PartChain[]>(
    "/orchestrator/inference/chains",
    20000,
  );
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [providerMessage, setProviderMessage] = useState("");
  const [providerAuth, setProviderAuth] = useState<
    Record<string, ProviderAuthState>
  >({});
  const [expandedPool, setExpandedPool] = useState<string | null>(null);
  const authWindows = useRef<Record<string, Window | null>>({});
  const providers = overview.data?.providers || [];
  const embeddings = overview.data?.embeddings;
  const embeddingsFor = (provider: any) =>
    isLocalEmbeddings(provider) && embeddings?.model ? embeddings.model : "";
  const totalCost = Number(
    costs.data?.total_cost_usd ?? metrics.data?.total_cost_usd ?? 0,
  );
  const pendingAuthIds = Object.values(providerAuth)
    .filter((state) => state.state === "starting" || state.state === "pending")
    .map((state) => state.provider_id)
    .sort()
    .join(",");

  const saveProvider = async (id: string) => {
    try {
      await post(`/web/api/providers/${id}`, { api_key: keys[id] });
      setKeys((current) => ({ ...current, [id]: "" }));
      setProviderMessage("Provider credentials saved and runtime refreshed.");
      await overview.reload();
    } catch (error) {
      setProviderMessage(String(error));
    }
  };

  const connectProvider = async (id: string) => {
    const popup = window.open(
      "about:blank",
      `north-${id}-auth`,
      "popup,width=560,height=760",
    );
    authWindows.current[id] = popup;
    if (popup)
      popup.document.body.textContent = "Preparing secure OpenAI login…";
    setProviderAuth((current) => ({
      ...current,
      [id]: {
        provider_id: id,
        state: "starting",
        configured: false,
        detail: "Preparing browser login…",
      },
    }));
    try {
      const state = await post<ProviderAuthState>(
        `/web/api/providers/${id}/auth`,
        {},
      );
      setProviderAuth((current) => ({ ...current, [id]: state }));
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
      setProviderAuth((current) => ({
        ...current,
        [id]: {
          provider_id: id,
          state: "error",
          configured: false,
          detail: String(error),
        },
      }));
    }
  };

  const disconnectProvider = async (id: string) => {
    try {
      const state = await api<ProviderAuthState>(
        `/web/api/providers/${id}/auth`,
        { method: "DELETE" },
      );
      setProviderAuth((current) => ({ ...current, [id]: state }));
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
          const state = await api<ProviderAuthState>(
            `/web/api/providers/${id}/auth`,
          );
          if (stopped) return;
          setProviderAuth((current) => ({ ...current, [id]: state }));
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
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [pendingAuthIds]);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Runtime"
        title="System"
        subtitle="Providers, how a model gets picked, what it costs, and whether north is healthy."
      />
      <div className="system-hero">
        <HealthIndicator variant="hero" />
      </div>
      <div className="metric-cards">
        <div>
          <span>Tasks · 30 days</span>
          <strong>{metrics.data?.total_tasks || 0}</strong>
        </div>
        <div>
          <span>Input tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_in || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Output tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_out || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Model cost · month</span>
          <strong>${totalCost.toFixed(4)}</strong>
        </div>
      </div>
      <div className="two-column">
        <Panel
          title="Providers"
          label={`${providers.filter((provider: any) => provider.configured).length} configured`}
        >
          {providerMessage && <div className="notice">{providerMessage}</div>}
          <div className="provider-list">
            {providers.map((provider: any) => {
              const auth = providerAuth[provider.id];
              const waiting =
                auth?.state === "starting" || auth?.state === "pending";
              const configured = auth ? auth.configured : provider.configured;
              return (
                <div className="provider-row" key={provider.id}>
                  {/* The trailing detail is whatever identifies the provider: the env var
                holding its key, "Browser login" for OAuth, or - for the on-device
                embedder, which has no key at all - the model it runs. Without the
                last case the row ended on a dangling separator. */}
                  <div>
                    <b>{provider.name}</b>
                    <small>
                      {[
                        provider.description,
                        provider.auth_kind === "oauth_pkce"
                          ? "Browser login"
                          : provider.env_key || embeddingsFor(provider),
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                  </div>
                  <div className="provider-controls">
                    <span
                      className={
                        configured
                          ? "provider-state configured"
                          : "provider-state"
                      }
                    >
                      {waiting
                        ? "Waiting for browser login"
                        : configured
                          ? `Ready ${auth?.account_hint || provider.credential_hint}`
                          : "Not configured"}
                    </span>
                    {provider.env_key && (
                      <div className="provider-edit">
                        <input
                          type="password"
                          placeholder="API key"
                          value={keys[provider.id] || ""}
                          onChange={(event) =>
                            setKeys((current) => ({
                              ...current,
                              [provider.id]: event.target.value,
                            }))
                          }
                        />
                        <button
                          className="ghost-button"
                          disabled={!keys[provider.id]}
                          onClick={() => saveProvider(provider.id)}
                        >
                          Save
                        </button>
                      </div>
                    )}
                    {provider.auth_kind === "oauth_pkce" && (
                      <div className="provider-auth">
                        <div className="provider-auth-actions">
                          <button
                            className={
                              configured ? "ghost-button" : "primary-button"
                            }
                            disabled={waiting}
                            onClick={() => connectProvider(provider.id)}
                          >
                            {waiting
                              ? "Waiting…"
                              : configured
                                ? "Reconnect"
                                : "Connect"}
                          </button>
                          {configured && (
                            <button
                              className="ghost-button"
                              onClick={() => disconnectProvider(provider.id)}
                            >
                              Disconnect
                            </button>
                          )}
                          {auth?.authorization_url && (
                            <a
                              href={auth.authorization_url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              Continue login
                            </a>
                          )}
                        </div>
                        {auth?.detail && (
                          <small
                            className={
                              auth.state === "error"
                                ? "provider-auth-error"
                                : "provider-auth-detail"
                            }
                          >
                            {auth.detail}
                          </small>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>
        {/* What actually picks a model. This panel used to show "model pools" -
          capability buckets recomputed for the panel alone, left over from the
          router that was deleted. Grouping is not selection: routing ranks a
          chain per part of a task and walks it in order, which is what this is. */}
        <Panel title="How a model is picked" label="Live chain per part">
          <div>
            {(chains.data || []).map((part) => (
              <div className="model-pool" key={part.part}>
                <button
                  className="model-pool-toggle"
                  onClick={() =>
                    setExpandedPool(
                      expandedPool === part.part ? null : part.part,
                    )
                  }
                >
                  <span>
                    <b>{part.part}</b>
                    <small>
                      {part.eligible} eligible · ranked by{" "}
                      {part.order_by.replace(/_/g, " ")}
                      {part.requires.length
                        ? ` · needs ${part.requires.join(", ")}`
                        : ""}
                    </small>
                  </span>
                  <span className="pool-availability">
                    {expandedPool === part.part
                      ? "Hide"
                      : `1. ${part.models[0]?.model || "—"}`}
                  </span>
                </button>
                {expandedPool === part.part && (
                  <div className="model-list">
                    {part.models.map((model, index) => (
                      <div
                        className={
                          model.available
                            ? "chain-row"
                            : "chain-row chain-skipped"
                        }
                        key={model.model}
                      >
                        <span className="chain-rank">{index + 1}</span>
                        <b>{model.model}</b>
                        <span className="chain-meta">
                          {model.providers.join(", ")}
                          {" · "}
                          {model.price
                            ? `$${model.price.toFixed(6)}/tok`
                            : "free"}
                          {" · "}score {model.score.toFixed(3)}
                        </span>
                        {/* Why north would walk past this rung right now. */}
                        {!model.available && (
                          <em className="chain-why">{model.skipped_because}</em>
                        )}
                      </div>
                    ))}
                    {!part.models.length && (
                      <Empty>Nothing qualifies for this part right now.</Empty>
                    )}
                  </div>
                )}
              </div>
            ))}
            {!chains.loading && !(chains.data || []).length && (
              <Empty>
                Routing is not ready - the model catalog has not loaded yet.
              </Empty>
            )}
          </div>
        </Panel>
      </div>
      <div className="two-column">
        <Panel title="Cost by model" label="Month to date">
          {Object.entries(costs.data?.by_model || {}).map(([name, value]) => (
            <div className="list-row" key={name}>
              <b>{name}</b>
              <span>${Number(value).toFixed(4)}</span>
            </div>
          ))}
          {!Object.keys(costs.data?.by_model || {}).length && (
            <Empty>No recorded inference costs yet.</Empty>
          )}
        </Panel>
        <Panel title="Runtime configuration">
          <div className="list-row">
            <b>Routing</b>
            <span>{overview.data?.settings?.routing || "–"}</span>
          </div>
          {/* Only shown when it is in force: a remembered pin that auto has released
            would read here as the model north is using, which it is not. */}
          {overview.data?.settings?.model && (
            <div className="list-row">
              <b>Pinned model</b>
              <span>{overview.data.settings.model}</span>
            </div>
          )}
          <div className="list-row">
            <b>Power</b>
            <span>
              {overview.data?.settings?.routing === "manual"
                ? "not in use"
                : overview.data?.settings?.power || "–"}
            </span>
          </div>
          <div className="list-row">
            <b>Autonomy</b>
            <span>{overview.data?.settings?.autonomy || "–"}</span>
          </div>
          <div className="list-row">
            <b>Bootstrap</b>
            <span>{overview.data?.bootstrap?.status || "–"}</span>
          </div>
          {/* Depends on a binary north cannot install for you, so the row carries
            what to do about it rather than only that it is missing. */}
          <div className="list-row">
            <b>Browser</b>
            <span title={overview.data?.browser?.detail || ""}>
              {overview.data?.browser?.state || "–"}
              {overview.data?.browser?.state === "unavailable" && (
                <small className="row-hint">
                  {overview.data.browser.detail}
                </small>
              )}
            </span>
          </div>
          {/* The one model north runs itself. Worth naming: every stored vector is
            stamped with it, so "which embeddings am I on?" decides which memories
            can still be compared with which. */}
          <div className="list-row">
            <div>
              <b>Embeddings</b>
              <small>
                {overview.data?.embeddings?.local
                  ? "on-device · no data leaves this machine"
                  : overview.data?.embeddings?.provider || "not configured"}
              </small>
            </div>
            <span className="pool-availability">
              {overview.data?.embeddings?.model || "–"}
            </span>
          </div>
        </Panel>
      </div>
    </div>
  );
}

export function Bootstrap({ embedded = false }: { embedded?: boolean }) {
  const resource = useResource<any>("/web/api/system", 5000);
  const [message, setMessage] = useState("");
  const bootstrap = resource.data?.bootstrap;
  const completed = new Set(
    (bootstrap?.completed || []).map((item: any) => item.path),
  );
  const candidates = bootstrap?.candidates || [];
  const start = async () => {
    setMessage("Starting bootstrap…");
    try {
      const result = await post<any>("/web/api/bootstrap", { paths: [] });
      setMessage(`${result.selected || 0} document(s) queued for bootstrap.`);
      await resource.reload();
    } catch (error) {
      setMessage(String(error));
    }
  };
  const content = (
    <>
      <div className="bootstrap-heading">
        <div>
          <div className="eyebrow">Bootstrap coverage</div>
          <h2>Documents North has read</h2>
          <p>
            Track eligible documents and refresh durable context in one batch.
          </p>
        </div>
        <button className="primary-button" onClick={start}>
          Run bootstrap
        </button>
      </div>
      <div className="metric-cards">
        <div>
          <span>Status</span>
          <strong>{bootstrap?.status || "–"}</strong>
        </div>
        <div>
          <span>Eligible documents</span>
          <strong>{bootstrap?.candidate_count || 0}</strong>
        </div>
        <div>
          <span>Completed documents</span>
          <strong>{completed.size}</strong>
        </div>
      </div>
      {message && <div className="notice">{message}</div>}
      <Panel title="Document coverage" label="Bootstrap sources">
        <div className="bootstrap-list">
          {candidates.map((path: string) => (
            <div className="bootstrap-row" key={path}>
              <span>
                <b>{path.split("/").pop()}</b>
                <small>{path}</small>
              </span>
              <Status value={completed.has(path) ? "completed" : "pending"} />
            </div>
          ))}
        </div>
        {!candidates.length && (
          <Empty>No eligible bootstrap documents found.</Empty>
        )}
      </Panel>
    </>
  );
  return embedded ? (
    <section className="bootstrap-embedded">{content}</section>
  ) : (
    <div className="page">{content}</div>
  );
}
