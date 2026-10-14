import { useEffect, useState } from "react";
import { api, del, patch, post } from "../api";
import { Empty, ErrorNotice, Loading, PageHeader, Panel, Status, timeAgo, reveal } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { Link, useSearchParams } from "react-router-dom";
import type { Approval, CardField, MemoryRef } from "../types";

export const fieldLabel = (field: CardField) =>
  field.label ||
  field.name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// One filled-in field. Read-only ones render as text so the card reads as work
// to check rather than a form to fill: the point is to see what North put there,
// and only the parts it offered as editable invite typing.
export function CardFieldRow({
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
export const REJECTION_REASONS = [
  "Not relevant",
  "Wrong details",
  "Already handled",
  "Bad timing",
  "Not interested",
];

export function ApprovalCard({
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

export type ApprovalTab = "waiting" | "decisions" | "rules";

// Everything about approving lives here: what is waiting, what you decided
// before (and so what north will decide the same way), and the actions north
// may take without asking.
export const APPROVAL_TABS: [ApprovalTab, string][] = [
  ["waiting", "Waiting"],
  ["decisions", "Past decisions"],
  ["rules", "Safe actions"],
];

export function Approvals() {
  const [params, setParams] = useSearchParams();
  const wanted = params.get("tab");
  const tab: ApprovalTab = APPROVAL_TABS.some(([name]) => name === wanted) ? (wanted as ApprovalTab) : "waiting";
  const find = params.get("find") || "";
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
  const overrule = async (
    card: Approval,
    decision: string,
    chosen_option: string,
    reason: string,
  ) => {
    await post("/orchestrator/approval/overrule", {
      card_id: card.id,
      decision,
      chosen_option,
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
      <nav className="segmented" aria-label="Approval views">
        {APPROVAL_TABS.map(([value, label]) => (
          <button
            key={value}
            className={tab === value ? "active" : ""}
            aria-pressed={tab === value}
            onClick={() => setParams(value === "waiting" ? {} : { tab: value })}
          >
            {label}
          </button>
        ))}
      </nav>
      {tab === "decisions" && <ApprovalMemoryPanel find={find} />}
      {tab === "rules" && <SafeActionsPanel />}
      {tab === "waiting" && <>
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
          <DecisionRow
            key={card.id}
            card={card}
            onOverrule={(d, o, r) => overrule(card, d, o, r)}
          />
        ))}
      </div>
      </>}
    </div>
  );
}

export function memoryLabel(ref: MemoryRef): string {
  const label = ref.label || ref.kind;
  return ref.text ? `${label}: ${ref.text}` : label;
}

// Where a piece of memory lives, so a decision can be traced back to what it
// read - and that memory corrected or forgotten there. Past decisions live with
// approvals; everything else on the Memory page.
export function memoryLink(ref: MemoryRef): string {
  const params = new URLSearchParams();
  if (ref.kind === "past_decision") {
    params.set("tab", "decisions");
    if (ref.ref) params.set("find", ref.ref);
    return `/approvals?${params.toString()}`;
  }
  // The profile is rebuilt from your facts, so it is corrected there.
  if (ref.kind === "fact" || ref.kind === "profile") params.set("tab", "facts");
  if (ref.kind === "episode") params.set("tab", "episodes");
  if (ref.kind === "judgement_rules") {
    params.set("tab", "documents");
    params.set("doc", "judgement_rules.md");
  }
  if (ref.text) params.set("find", ref.text);
  return `/memory?${params.toString()}`;
}

export function approveLabel(options: string[]): string {
  return (
    options.find((option) =>
      /^(approve|apply|run|yes|allow|proceed)$/i.test(option),
    ) ||
    options[0] ||
    "Approve"
  );
}

export function DecisionDetail({
  label,
  reason,
  memory,
}: {
  label: string;
  reason: string;
  memory: MemoryRef[];
}) {
  if (!label && !reason) return null;
  return (
    <div className="decision-detail">
      <small>
        {label && `Decided by ${label}`}
        {label && reason && ": "}
        {reason}
      </small>
      {memory.length > 0 && (
        <ul className="decision-memory">
          {memory.map((ref, index) => (
            <li key={index}>
              <Link to={memoryLink(ref)}>{memoryLabel(ref)}</Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// A decided card: who decided, why, and what it read - and, for a decision
// north took, a way to overrule it. Overruling teaches memory so north decides
// your way next time; it cannot undo what already happened, and says so.
export function DecisionRow({
  card,
  onOverrule,
}: {
  card: Approval;
  onOverrule: (
    decision: string,
    chosen_option: string,
    reason: string,
  ) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [answer, setAnswer] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const northDecided = Boolean(card.decided_by) && card.decided_by !== "you";
  const question = card.type === "question";
  const flipped = card.status === "approved" ? "rejected" : "approved";
  const submit = async () => {
    setError("");
    try {
      if (question) await onOverrule("answered", answer.trim(), reason.trim());
      else
        await onOverrule(
          flipped,
          flipped === "approved" ? approveLabel(card.options) : "",
          reason.trim(),
        );
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  const prior = card.overruled;
  return (
    <div className="decision-row">
      <div className="table-row">
        <div className="row-main">
          <b>{card.title}</b>
          <small>
            {card.agent} · {timeAgo(card.created_at)}
            {question && card.chosen_option && ` · "${card.chosen_option}"`}
          </small>
        </div>
        <Status value={card.status} />
      </div>
      <DecisionDetail
        label={card.decided_by_label}
        reason={card.reason}
        memory={card.memory_used || []}
      />
      {prior && (
        <div className="decision-overruled">
          <small>
            You overruled north, which had {prior.status}
            {prior.chosen_option && ` "${prior.chosen_option}"`}.
          </small>
          <DecisionDetail
            label={prior.decided_by_label}
            reason={prior.reason}
            memory={prior.memory_used || []}
          />
        </div>
      )}
      {northDecided && !open && (
        <button className="ghost-button" onClick={() => setOpen(true)}>
          Overrule
        </button>
      )}
      {open && (
        <div className="overrule-form">
          <p className="muted">
            North will decide your way next time. What it already did, or
            refused, stays done.
          </p>
          {question && (
            <input
              placeholder="Your answer"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
          )}
          <input
            placeholder="Why (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="card-actions">
            <button
              className="primary-button"
              disabled={question && !answer.trim()}
              onClick={submit}
            >
              {question
                ? "Answer instead"
                : flipped === "approved"
                  ? "Should have approved"
                  : "Should have rejected"}
            </button>
            <button className="ghost-button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
          {error && <ErrorNotice message={error} />}
        </div>
      )}
    </div>
  );
}

export interface ApprovalDecision {
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
export function ApprovalMemoryPanel({ find = "" }: { find?: string }) {
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
        comes up. Safe and autonomous modes replay these before anything else.
      </p>
      {resource.loading ? (
        <Loading />
      ) : rows.length ? (
        rows.map((row) => (
          <div
            className={`memory-row${row.fingerprint === find ? " memory-found" : ""}`}
            key={row.fingerprint}
            ref={row.fingerprint === find ? reveal : undefined}
          >
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

export interface UnattendedRule {
  id: string;
  kind: string;
  pattern: string;
  enabled: boolean;
  source: string;
  note: string;
  fire_count: number;
  last_fired_at: string | null;
}
export interface UnattendedRules {
  mode: string;
  active: boolean;
  kinds: string[];
  rules: UnattendedRule[];
}

export const KIND_LABELS: Record<string, string> = {
  command: "Commands",
  git: "Git actions",
  device: "Device toggles",
  self_message: "Messages to you",
  browser: "Browser: pages and fields",
};

export const KIND_HELP: Record<string, string> = {
  command:
    "Run without asking. Matched as a whole command or a prefix; chaining, pipes and redirects are always refused.",
  git: "Local, reversible git only. Push, pull, merge and forced variants are never auto-approved.",
  device:
    "Trivially reversible physical actions, where the undo is another toggle.",
  self_message:
    "Messages addressed to you. A message to anyone else is never auto-approved.",
  browser:
    "Opening pages and filling fields, so a drafted form does not stop at every field. Off until you turn it on: a page can read what is typed before anything is submitted. A click or key press always asks.",
};

// The safe-action list north runs in `safe` mode without asking. It used to be a
// tuple in a source file: you could not see it, add to it, or take anything out
// of it. approval_memory next door already settled the principle - a decision
// that cannot be withdrawn is not consent - and a hardcoded allowlist fails it.
export function SafeActionsPanel() {
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

      {/* A rule only ever fires in `safe`. Listing rules that cannot fire, with
        nothing saying so, is how someone concludes the feature is broken. */}
      {data && !data.active && (
        <div className="notice">
          These rules are inactive: north is in <b>{data.mode}</b> mode. They
          apply only in <b>safe</b> mode.
        </div>
      )}

      <p className="muted memory-note">
        What north may do without asking, in safe mode. Two things are never on
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

