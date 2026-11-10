import { useEffect, useState } from "react";
import { del, patch, post } from "../api";
import { Markdown, NoticeList, PageHeader, Panel, timeAgo } from "../components";
import { ActionBar, Button, confirmDelete, Empty, EntityForm, Field, ListRow, Loading, Notice, ResourceView, SectionHeader, Stack, Status, Tabs, Note } from "../kit";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { Link, useSearchParams } from "react-router-dom";
import type { Approval, MemoryRef, Notice as NoticeEntry } from "../types";
import { ApprovalCard } from "./approvalCard";

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
  const notices = useResource<NoticeEntry[]>("/web/api/notices", 30000);
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
    <Stack>
      {cards.map((card) => (
        <ApprovalCard
          key={card.id}
          card={card}
          wide
          onDecide={(d, o, v, r) => decide(card, d, o, v, r)}
        />
      ))}
    </Stack>
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
      {resource.error && <Notice tone="error">{resource.error}</Notice>}
      <Tabs
        label="Approval views"
        items={APPROVAL_TABS.map(([value, label]) => ({ value, label }))}
        value={tab}
        onChange={(value) => setParams(value === "waiting" ? {} : { tab: value })}
      />
      {tab === "decisions" && <ApprovalMemoryPanel find={find} />}
      {tab === "rules" && <SafeActionsPanel />}
      {tab === "waiting" && resource.loading && <Loading />}
      {tab === "waiting" && !resource.loading && <>
      {(notices.data || []).length > 0 && (
        <>
          <SectionHeader
            title="From north"
            description="Reminders and how scheduled runs ended. Nothing to decide; dismiss each once read."
          />
          <div className="table-list"><NoticeList notices={notices.data || []} onDismissed={() => void notices.reload()}/></div>
        </>
      )}
      {blocking.length > 0 && (
        <>
          <SectionHeader title="Needs you now" description="Something is stopped until you answer." />
          {section(blocking)}
        </>
      )}

      {prepared.length > 0 && (
        <>
          <SectionHeader
            title="When you have a minute"
            description="Work north finished and left for you. Nothing is blocked; take the time to read it."
          />
          {section(prepared)}
        </>
      )}

      {!pending.length && !(notices.data || []).length && <Empty>Nothing needs your attention.</Empty>}

      <Panel title="Resolved" label={`${history.length} decided`}>
        {history.length ? (
          history.map((card) => (
            <DecisionRow
              key={card.id}
              card={card}
              onOverrule={(d, o, r) => overrule(card, d, o, r)}
            />
          ))
        ) : (
          <Empty>Nothing decided yet.</Empty>
        )}
      </Panel>
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
  const [busy, setBusy] = useState(false);
  const northDecided = Boolean(card.decided_by) && card.decided_by !== "you";
  const question = card.type === "question";
  const flipped = card.status === "approved" ? "rejected" : "approved";
  const submit = async () => {
    setError("");
    setBusy(true);
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
    } finally {
      setBusy(false);
    }
  };
  const prior = card.overruled;
  return (
    <ListRow
      title={card.title}
      meta={[
        card.agent,
        timeAgo(card.created_at),
        question && card.chosen_option && `"${card.chosen_option}"`,
      ]}
      status={<Status value={card.status} />}
      actions={
        northDecided && !open ? (
          <Button variant="quiet" size="sm" onClick={() => setOpen(true)}>
            Overrule
          </Button>
        ) : undefined
      }
    >
      {/* What was decided: the command, the path, the diff. A row with only "Git Operation -
        Approval Required · Decided by the safe list" gave nothing to judge an overrule by. */}
      {card.message && (
        <div className="decision-what">
          <Markdown>{card.message}</Markdown>
        </div>
      )}
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
      {open && (
        <EntityForm
          title="Overrule north"
          description="North will decide your way next time. What it already did, or refused, stays done."
          submitLabel={
            question
              ? "Answer instead"
              : flipped === "approved"
                ? "Should have approved"
                : "Should have rejected"
          }
          canSubmit={!question || Boolean(answer.trim())}
          busy={busy}
          error={error}
          onSubmit={submit}
          onCancel={() => setOpen(false)}
        >
          {question && (
            <Field label="Your answer">
              <input
                placeholder="Your answer"
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
              />
            </Field>
          )}
          <Field label="Why (optional)">
            <input
              placeholder="Why (optional)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
        </EntityForm>
      )}
    </ListRow>
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
  const dialog = useDialog();
  const resource = useResource<ApprovalDecision[]>(
    "/web/api/memory/approvals",
    10000,
  );
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const forget = async (row: ApprovalDecision) => {
    const ok = await confirmDelete(dialog, {
      noun: "decision",
      name: row.signature,
      verb: "Forget",
      consequence: "North will ask you again the next time this action comes up.",
    });
    if (!ok) return;
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
      {error && <Notice tone="error" onDismiss={() => setError("")}>{error}</Notice>}
      <Note>
        Every time you approve or reject an action, north records it against a
        fingerprint of that action and replays your decision when a matching one
        comes up. Safe and autonomous modes replay these before anything else.
      </Note>
      <ResourceView
        resource={resource}
        isEmpty={(data) => !data.length}
        empty="Nothing learned yet. Approve or reject an action and it appears here."
      >
        {(data) =>
          data.map((row) => (
              <ListRow
                key={row.fingerprint}
                found={row.fingerprint === find}
                title={row.signature}
                meta={[
                  row.agent,
                  row.count > 1 && `${row.count}×`,
                  timeAgo(row.updated_at),
                ]}
                status={<Status value={row.decision} />}
                actions={
                  <Button
                    variant="danger"
                    size="sm"
                    busy={busy === row.fingerprint}
                    busyLabel="Forgetting…"
                    onClick={() => void forget(row)}
                  >
                    Forget
                  </Button>
                }
              />
          ))
        }
      </ResourceView>
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
  // A shipped rule can be added back ("Restore shipped rules"), so it is removed;
  // a rule of your own is deleted.
  const remove = async (row: UnattendedRule) => {
    const shipped = row.source === "builtin";
    const ok = await confirmDelete(dialog, {
      noun: "rule",
      name: row.pattern,
      verb: shipped ? "Remove" : "Delete",
      consequence: shipped
        ? "It stays in the list, switched off, so you can restore it later."
        : "This rule is yours, so deleting it cannot be undone.",
    });
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
    >
      {error && <Notice tone="error" onDismiss={() => setError("")}>{error}</Notice>}

      {/* A rule only ever fires in `safe`. Listing rules that cannot fire, with
        nothing saying so, is how someone concludes the feature is broken. */}
      {data && !data.active && (
        <Notice tone="warning">
          These rules are inactive: north is in <b>{data.mode}</b> mode. They
          apply only in <b>safe</b> mode.
        </Notice>
      )}

      <Note>
        What north may do without asking, in safe mode. Two things are never on
        this list, whatever you add: sending something to another person, and
        spending money. A sent email cannot be unsent.
      </Note>

      <EntityForm
        title="Add a safe action"
        submitLabel="Add rule"
        busy={busy === "new"}
        busyLabel="Adding…"
        canSubmit={Boolean(pattern.trim())}
        onSubmit={add}
      >
        <Field label="Kind">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k] || k}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Action">
          <input
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            placeholder={kind === "command" ? "e.g. make lint" : "e.g. fetch"}
          />
        </Field>
      </EntityForm>

      <ResourceView
        resource={resource}
        isEmpty={(loaded) => !loaded.rules.length}
        empty="No safe actions configured."
      >
        {() => <Stack gap="lg">{
          kinds.map((k) => {
            const forKind = rules.filter((r) => r.kind === k);
            if (!forKind.length) return null;
            return (
              <Stack gap="sm" key={k}>
                <SectionHeader level={3} title={KIND_LABELS[k] || k} description={KIND_HELP[k]} />
                <div>{forKind.map((row) => (
                  <ListRow
                    key={row.id}
                    title={row.pattern}
                    muted={!row.enabled}
                    meta={[
                      row.source === "builtin" ? "shipped" : "yours",
                      !row.enabled && "disabled",
                      // A rule that has fired 40 times is a different object from one
                      // that never has, and that is what you want to see before keeping it.
                      row.fire_count > 0
                        ? `used ${row.fire_count}×${row.last_fired_at ? `, last ${timeAgo(row.last_fired_at)}` : ""}`
                        : "never used",
                    ]}
                    actions={
                      <>
                        <Button
                          variant="quiet"
                          size="sm"
                          disabled={busy === row.id}
                          onClick={() => void toggle(row)}
                        >
                          {row.enabled ? "Disable" : "Enable"}
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={busy === row.id}
                          onClick={() => void remove(row)}
                        >
                          {row.source === "builtin" ? "Remove" : "Delete"}
                        </Button>
                      </>
                    }
                  />
                ))}</div>
              </Stack>
            );
          })
        }</Stack>}
      </ResourceView>

      <ActionBar>
        <Button busy={busy === "restore"} busyLabel="Restoring…" onClick={() => void restore()}>
          Restore shipped rules
        </Button>
      </ActionBar>
    </Panel>
  );
}
