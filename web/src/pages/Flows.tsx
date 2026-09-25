import { useEffect, useState } from "react";
import { api, del, patch, post } from "../api";
import {
  Empty,
  ErrorNotice,
  formatDateTime,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { agoText, whenFromNow } from "../schedule";
import { ConfirmRow, draftOf, emptyDraft, ScheduleForm } from "../scheduleForm";
import type { Draft } from "../scheduleForm";
import {
  buildUpcoming,
  cadenceText,
  flowActions,
  flowDeletionPrompt,
  flowScheduleBody,
  flowStepsComplete,
  lastRunOf,
  lastRunText,
  progressText,
  scheduleSummary,
  schedulesByFlow,
  triggerLabel,
  usesSystemAction,
} from "./flowsView";
import type { JobLike, RunLike, ScheduleLike } from "./flowsView";
import { CapabilityModal } from "./Verbose";
import type { SkillSummary } from "./Verbose";

interface FlowSummary {
  name: string;
  description: string;
  source: string;
  status: string;
  domains: string[];
  steps: number;
}
interface FlowStepDraft {
  name: string;
  skill: string;
  instructions: string;
  inputs: Record<string, unknown>;
  // Set only on a built-in flow's step that runs one of north's own jobs.
  action?: string;
}
interface FlowDetail {
  name: string;
  description: string;
  status: string;
  domains: string[];
  source: string;
  tested_run_id: string;
  steps: FlowStepDraft[];
}
interface FlowDraft {
  name: string;
  description: string;
  status: string;
  domains: string[];
  steps: FlowStepDraft[];
}

const emptyStep = (): FlowStepDraft => ({
  name: "",
  skill: "",
  instructions: "",
  inputs: {},
});
const emptyFlow = (): FlowDraft => ({
  name: "",
  description: "",
  status: "candidate",
  domains: ["general"],
  steps: [emptyStep()],
});
const draftFromDetail = (detail: FlowDetail): FlowDraft => ({
  name: detail.name,
  description: detail.description,
  status: detail.status,
  domains: detail.domains,
  steps: detail.steps.map((step) => ({
    ...emptyStep(),
    ...step,
    inputs: step.inputs || {},
  })),
});

function FlowStepCard({
  step,
  index,
  onChange,
  onRemove,
  canRemove,
  editable,
  skills,
}: {
  step: FlowStepDraft;
  index: number;
  onChange: (step: FlowStepDraft) => void;
  onRemove: () => void;
  canRemove: boolean;
  editable: boolean;
  skills: SkillSummary[];
}) {
  const inputs = Object.entries(step.inputs || {});
  const set = (patch: Partial<FlowStepDraft>) =>
    onChange({ ...step, ...patch });
  const setInput = (oldKey: string, key: string, value: unknown) => {
    const next = { ...step.inputs };
    if (oldKey !== key) delete next[oldKey];
    next[key] = value;
    set({ inputs: next });
  };
  const activeSkills = skills.filter((skill) => skill.status === "active");
  const selectedSkill = skills.find((skill) => skill.name === step.skill);
  const execution = selectedSkill?.execution;
  // A step is one kind or the other - never both - so this is derived from
  // step.skill rather than tracked as separate state that could drift out of
  // sync with it. "Existing skill" always leaves with a real skill selected
  // (see chooseSkillKind below), so an empty step.skill unambiguously means
  // "instructions"; there is no third "chosen skill, picked nothing yet" state
  // to represent.
  const kind: "skill" | "instructions" = step.skill ? "skill" : "instructions";
  const defaultInputsFor = (contract: typeof execution) => {
    const required = contract?.inputs.required || [];
    const properties = contract?.inputs.properties || {};
    return Object.fromEntries(
      required.map((key) => [
        key,
        properties[key]?.type === "boolean" ? false : "",
      ]),
    );
  };
  const selectSkill = (name: string) => {
    const contract = skills.find((skill) => skill.name === name)?.execution;
    set({ skill: name, inputs: defaultInputsFor(contract) });
  };
  // Switching kind clears the other field rather than leaving it hidden but
  // still set: a step submitted mid-edit should unambiguously be one kind,
  // not a skill with orphaned leftover instructions (or vice versa).
  const chooseSkillKind = () => {
    const first = activeSkills[0];
    if (!first) return;
    set({
      skill: first.name,
      instructions: "",
      inputs: defaultInputsFor(first.execution),
    });
  };
  const chooseInstructionsKind = () => set({ skill: "", inputs: {} });
  return (
    <article className="flow-step-card">
      <header>
        <div>
          <span className="eyebrow">Step {index + 1}</span>
          <h3>{step.name || "Untitled step"}</h3>
        </div>
        <button
          type="button"
          className="ghost-button danger-link"
          disabled={!canRemove}
          onClick={onRemove}
        >
          Remove
        </button>
      </header>
      {step.action ? (
        <div className="flow-contract">
          <span>
            <b>Runs</b>
            north's own job
          </span>
          <span>
            <b>Action</b>
            {step.action}
          </span>
          <span>
            <b>Model</b>
            none, it is plain code
          </span>
        </div>
      ) : null}
      {!step.action && (
        <div className="flow-step-grid">
          <label>
            Step name
            <input
              disabled={!editable}
              value={step.name}
              placeholder="Review matching jobs"
              onChange={(event) => set({ name: event.target.value })}
            />
          </label>
          <label>
            Kind
            <div className="segmented">
              <button
                type="button"
                className={kind === "skill" ? "active" : ""}
                disabled={!editable || !activeSkills.length}
                title={
                  activeSkills.length
                    ? undefined
                    : "No active skills to choose from"
                }
                onClick={chooseSkillKind}
              >
                Existing skill
              </button>
              <button
                type="button"
                className={kind === "instructions" ? "active" : ""}
                disabled={!editable}
                onClick={chooseInstructionsKind}
              >
                Instructions
              </button>
            </div>
          </label>
        </div>
      )}
      {step.action ? null : kind === "skill" ? (
        <label className="flow-wide-field">
          Skill
          <select
            aria-label="Skill"
            disabled={!editable}
            value={step.skill}
            onChange={(event) => selectSkill(event.target.value)}
          >
            {step.skill &&
              !activeSkills.some((skill) => skill.name === step.skill) && (
                <option value={step.skill}>{step.skill} (unavailable)</option>
              )}
            {activeSkills.map((skill) => (
              <option key={skill.name} value={skill.name}>
                {skill.name}
                {skill.execution ? "" : " · instruction only"}
              </option>
            ))}
          </select>
          <small>North runs this step using the selected reusable skill.</small>
        </label>
      ) : (
        <label className="flow-wide-field">
          Step instructions
          <textarea
            aria-label="Step instructions"
            disabled={!editable}
            value={step.instructions}
            rows={3}
            placeholder="Write the procedure North should follow for this step."
            onChange={(event) => set({ instructions: event.target.value })}
          />
          <small>
            North follows these instructions directly - no reusable skill
            involved.
          </small>
        </label>
      )}
      {execution && (
        <div className="flow-contract">
          <span>
            <b>Executor</b>
            {execution.agent}
          </span>
          <span>
            <b>Allowed tools</b>
            {execution.tools.length ? execution.tools.join(", ") : "None"}
          </span>
          <span>
            <b>Approval</b>
            {execution.approval === "always"
              ? "Whole step"
              : execution.approval === "on_mutation"
                ? "Before mutations"
                : "None"}
          </span>
        </div>
      )}
      {execution && (
        <div className="flow-params">
          <div className="flow-params-heading">
            <div>
              <b>Skill inputs</b>
              <small>
                Required fields are loaded from the skill contract. Bind prior
                results with {"${steps.review.output}"}.
              </small>
            </div>
            <button
              type="button"
              className="ghost-button"
              disabled={!editable}
              onClick={() => set({ inputs: { ...step.inputs, "": "" } })}
            >
              + Add input
            </button>
          </div>
          {inputs.length ? (
            inputs.map(([key, value], inputIndex) => {
              const property = execution.inputs.properties?.[key];
              return (
                <div className="flow-param-row" key={`${key}-${inputIndex}`}>
                  <input
                    disabled={!editable}
                    aria-label="Input name"
                    placeholder="input name"
                    value={key}
                    onChange={(event) =>
                      setInput(key, event.target.value, value)
                    }
                  />
                  {property?.enum ? (
                    <select
                      disabled={!editable}
                      aria-label={`${key} value`}
                      value={String(value ?? "")}
                      onChange={(event) =>
                        setInput(key, key, event.target.value)
                      }
                    >
                      <option value="">Select value</option>
                      {property.enum.map((option) => (
                        <option key={String(option)} value={String(option)}>
                          {String(option)}
                        </option>
                      ))}
                    </select>
                  ) : property?.type === "boolean" ? (
                    <select
                      disabled={!editable}
                      aria-label={`${key} value`}
                      value={String(value)}
                      onChange={(event) =>
                        setInput(key, key, event.target.value === "true")
                      }
                    >
                      <option value="false">No</option>
                      <option value="true">Yes</option>
                    </select>
                  ) : (
                    <input
                      disabled={!editable}
                      aria-label="Input value"
                      placeholder="value or reference"
                      value={String(value ?? "")}
                      onChange={(event) =>
                        setInput(key, key, event.target.value)
                      }
                    />
                  )}
                  <button
                    type="button"
                    className="ghost-button danger-link"
                    disabled={!editable}
                    aria-label="Remove input"
                    onClick={() => {
                      const next = { ...step.inputs };
                      delete next[key];
                      set({ inputs: next });
                    }}
                  >
                    ×
                  </button>
                </div>
              );
            })
          ) : (
            <small className="flow-empty-inputs">No skill inputs needed.</small>
          )}
        </div>
      )}
    </article>
  );
}

interface FlowRunStep {
  step: string;
  skill: string;
  agent: string;
  summary: string;
  output: string;
  tools_used: string[];
  artifacts: { name: string; kind: string }[];
}
interface FlowRunView extends RunLike {
  steps: FlowRunStep[];
}
interface ScheduleView extends ScheduleLike {
  label: string;
  task: string;
  agent: string;
  weekdays: number[];
  schedule: string;
  source: string;
}

// A step that answered with nothing worth quoting still did something: say so
// from what it left behind, and only claim it recorded nothing when it did.
const stepFallback = (step: FlowRunStep) =>
  step.artifacts.length || step.tools_used.length
    ? "Finished."
    : "No output recorded.";

// One run's row, opening to what each step did. Used under "Recent runs" and in
// a flow's own history, so a run reads the same wherever it is listed.
function RunRow({
  run,
  showFlow,
  open,
  onToggle,
}: {
  run: FlowRunView;
  showFlow: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const how = triggerLabel(run.trigger);
  return (
    <div className="schedule-row run-row">
      <div className="schedule-main">
        <b>{showFlow ? run.flow : how || "run"}</b>
        <small title={formatDateTime(run.started_at)}>
          {[showFlow ? how : "", agoText(run.started_at), progressText(run)]
            .filter(Boolean)
            .join(" · ")}
        </small>
        {run.error && <small className="run-error">{run.error}</small>}
        {open && (
          <dl className="run-details">
            {run.steps.length ? (
              run.steps.map((step) => (
                <div key={step.step}>
                  <dt>
                    {step.step}
                    {step.agent && ` · ${step.agent}`}
                  </dt>
                  <dd>
                    {step.summary || step.output || stepFallback(step)}
                    {step.artifacts.length > 0 &&
                      `\nWrote ${step.artifacts.map((file) => `${file.kind}/${file.name}`).join(", ")}`}
                    {step.tools_used.length > 0 &&
                      `\nUsed ${step.tools_used.join(", ")}`}
                  </dd>
                </div>
              ))
            ) : (
              <div>
                <dt>Steps</dt>
                <dd>No step finished before this run stopped.</dd>
              </div>
            )}
          </dl>
        )}
      </div>
      <div className="schedule-actions">
        <Status value={run.status} />
        <button
          className="ghost-button"
          aria-expanded={open}
          onClick={onToggle}
        >
          {open ? "Hide" : "Details"}
        </button>
      </div>
    </div>
  );
}

// A flow's schedules, managed where the flow is: when it runs is a property of
// the flow, so it is added, retimed, paused and removed here. There is no prompt
// and no agent to choose - the flow carries the work.
function FlowSchedules({
  flow,
  entries,
  oneOffs,
  canSchedule,
  onChanged,
}: {
  flow: string;
  entries: ScheduleView[];
  oneOffs: JobLike[];
  canSchedule: boolean;
  onChanged: () => Promise<unknown>;
}) {
  const [editing, setEditing] = useState("");
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState("");
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // One place decides what a failure looks like: the message shows beside the
  // form, and the lists are re-read so nothing keeps a guessed state.
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await work();
      await onChanged();
      setEditing("");
      setCreating(false);
      setConfirming("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const close = () => {
    setEditing("");
    setCreating(false);
    setConfirming("");
    setError("");
  };
  const startCreate = () => {
    close();
    setDraft(emptyDraft());
    setCreating(true);
  };
  const startEdit = (entry: ScheduleView) => {
    close();
    setDraft(draftOf(entry));
    setEditing(entry.name);
  };
  const path = (name: string) =>
    `/web/api/flow-schedules/${encodeURIComponent(name)}`;

  return (
    <>
      <div className="flow-schedule-head">
        <small>
          {canSchedule
            ? "When this flow runs. Each schedule runs the whole flow."
            : "Test and activate this flow before scheduling it."}
        </small>
        <button
          className="ghost-button"
          onClick={startCreate}
          disabled={!canSchedule || busy}
        >
          + Add schedule
        </button>
      </div>
      {creating && (
        <ScheduleForm
          draft={draft}
          setDraft={setDraft}
          allowOnce
          busy={busy}
          error={error}
          submitLabel={draft.repeat === "once" ? "Schedule it" : "Add schedule"}
          onCancel={close}
          onSubmit={() =>
            act(() =>
              post(
                `/web/api/flow-definitions/${encodeURIComponent(flow)}/schedules`,
                flowScheduleBody(draft),
              ),
            )
          }
        />
      )}
      {!entries.length && !oneOffs.length && !creating && (
        <Empty>This flow is not scheduled. It runs only when asked.</Empty>
      )}
      {oneOffs.map((job) => (
        <div className="schedule-row" key={job.job_id}>
          <div className="schedule-main">
            <b>Once</b>
            <small>{job.scheduled_local}</small>
          </div>
          <div className="schedule-actions">
            <button
              className="ghost-button"
              disabled={busy}
              onClick={() =>
                act(() =>
                  del(`/orchestrator/jobs/${encodeURIComponent(job.job_id)}`),
                )
              }
            >
              Cancel
            </button>
          </div>
        </div>
      ))}
      {entries.map((entry) => (
        <div
          className={entry.enabled ? "schedule-row" : "schedule-row paused"}
          key={entry.name}
        >
          <div className="schedule-main">
            <b>{entry.title}</b>
            <small>
              {cadenceText(entry)}
              {entry.enabled ? ` · next ${entry.next_run_local}` : " · paused"}
            </small>
          </div>
          {confirming === entry.name ? (
            <ConfirmRow
              busy={busy}
              question="Delete this schedule? The flow stays; it just stops running on this schedule."
              confirmLabel="Delete"
              onCancel={() => setConfirming("")}
              onConfirm={() => act(() => del(path(entry.name)))}
            />
          ) : (
            <div className="schedule-actions">
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() =>
                  act(() =>
                    patch(path(entry.name), { enabled: !entry.enabled }),
                  )
                }
              >
                {entry.enabled ? "Pause" : "Resume"}
              </button>
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() => startEdit(entry)}
              >
                Edit
              </button>
              <button
                className="ghost-button danger-link"
                disabled={busy}
                onClick={() => setConfirming(entry.name)}
              >
                Delete
              </button>
            </div>
          )}
          {editing === entry.name && (
            <ScheduleForm
              draft={draft}
              setDraft={setDraft}
              allowOnce={false}
              busy={busy}
              error={error}
              submitLabel="Save changes"
              onCancel={close}
              onSubmit={() =>
                act(() => patch(path(entry.name), flowScheduleBody(draft)))
              }
            />
          )}
        </div>
      ))}
      {error && !creating && !editing && (
        <p className="schedule-form-error">{error}</p>
      )}
    </>
  );
}

type FlowTab = "definition" | "schedule" | "runs";

export function Flows() {
  const dialog = useDialog();
  const flows = useResource<FlowSummary[]>("/web/api/flow-definitions", 10000);
  const skills = useResource<SkillSummary[]>("/web/api/skills", 10000);
  const cron = useResource<ScheduleView[]>("/orchestrator/cron", 10000);
  const jobs = useResource<JobLike[]>("/orchestrator/jobs?limit=50", 10000);
  const runs = useResource<FlowRunView[]>(
    "/web/api/flow-runs?limit=100",
    10000,
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<FlowDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState("");
  const [tab, setTab] = useState<FlowTab>("definition");
  const [openRun, setOpenRun] = useState("");
  // Quicker while a flow is open: a run started here should be seen moving.
  const history = useResource<FlowRunView[]>(
    selected
      ? `/web/api/flow-runs?flow=${encodeURIComponent(selected)}&limit=50`
      : null,
    selected ? 4000 : 0,
  );
  const [testedRun, setTestedRun] = useState("");
  // Whether a candidate has a passing test is the server's call, made against
  // the exact definition. Ask again whenever a run of this flow changes state,
  // so "Activate" appears the moment a test finishes rather than on reopening.
  const runStates = (history.data || [])
    .map((run) => `${run.run_id}:${run.status}`)
    .join(",");
  useEffect(() => {
    if (!selected || creating || !runStates) return;
    let current = true;
    api<FlowDetail>(`/web/api/flow-definitions/${encodeURIComponent(selected)}`)
      .then((detail) => {
        if (current) setTestedRun(detail.tested_run_id || "");
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [selected, creating, runStates]);

  const startCreating = (prefill?: FlowDraft) => {
    setCreating(true);
    setSelected(null);
    setDraft(prefill || emptyFlow());
    setMessage("");
  };
  const closeEditor = () => {
    setSelected(null);
    setDraft(null);
  };
  const open = async (name: string) => {
    setSelected(name);
    setCreating(false);
    setDraft(null);
    setMessage("");
    setTab("definition");
    try {
      const detail = await api<FlowDetail>(
        `/web/api/flow-definitions/${encodeURIComponent(name)}`,
      );
      setDraft(draftFromDetail(detail));
      setTestedRun(detail.tested_run_id || "");
    } catch (error) {
      setSelected(null);
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const reloadSchedules = () => Promise.all([cron.reload(), jobs.reload()]);
  // A run goes on in the background and can take minutes, so this returns as
  // soon as it has started and the Runs tab shows it moving.
  const startRun = async (mode: "test" | "execute") => {
    if (!selected) return;
    if (
      mode === "execute" &&
      !(await dialog.confirm(
        `Run '${selected}' now? It does the real work, and anything that needs approval will ask you under Approvals.`,
        { title: "Run flow?", confirmLabel: "Run now" },
      ))
    )
      return;
    try {
      await post(
        `/web/api/flow-definitions/${encodeURIComponent(selected)}/runs`,
        { mode },
      );
      setTab("runs");
      setMessage(
        mode === "test"
          ? "Test run started. It does the real work once, on a small case. Anything that needs approval is under Approvals."
          : "Run started. Anything that needs approval is under Approvals.",
      );
      await Promise.all([history.reload(), runs.reload()]);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const activate = async () => {
    if (!selected || !testedRun) return;
    if (
      !(await dialog.confirm(
        `Activate '${selected}'? It passed its test run, and once active it can run on a schedule and by hand. Changing it later returns it to candidate.`,
        { title: "Activate flow?", confirmLabel: "Activate" },
      ))
    )
      return;
    try {
      const detail = await post<FlowDetail>(
        `/web/api/flow-definitions/${encodeURIComponent(selected)}/activate`,
        { test_run_id: testedRun },
      );
      setDraft(draftFromDetail(detail));
      setTestedRun("");
      setMessage("Flow activated. It can now be scheduled and run.");
      await flows.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const create = async () => {
    if (!draft) return;
    try {
      const detail = await api<FlowDetail>("/web/api/flow-definitions", {
        method: "POST",
        body: JSON.stringify(draft),
      });
      setSelected(detail.name);
      setCreating(false);
      setDraft(draftFromDetail(detail));
      setMessage("Flow candidate created. Test it before activation.");
      await flows.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const save = async () => {
    if (!selected || !draft) return;
    try {
      const detail = await api<FlowDetail>(
        `/web/api/flow-definitions/${encodeURIComponent(selected)}`,
        { method: "PUT", body: JSON.stringify(draft) },
      );
      setDraft(draftFromDetail(detail));
      setTestedRun(detail.tested_run_id || "");
      setMessage("Flow saved as a candidate. Test it before activation.");
      await flows.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      flows.data?.find((flow) => flow.name === selected)?.source !==
        "learned" ||
      !(await dialog.confirm(
        flowDeletionPrompt(
          selected,
          (byFlow.get(selected) || []).map((entry) => entry.title),
          (jobs.data || []).filter(
            (job) =>
              job.flow === selected &&
              !job.cron_entry &&
              job.status === "queued",
          ).length,
        ),
        { title: "Delete flow?", confirmLabel: "Delete flow", danger: true },
      ))
    )
      return;
    try {
      await del(`/web/api/flow-definitions/${encodeURIComponent(selected)}`);
      setSelected(null);
      setDraft(null);
      setMessage("Learned flow deleted.");
      await flows.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  // A built-in flow is part of how north works, so it is shown, not edited:
  // saving over one used to write a same-named copy that then needed testing
  // again before anything scheduled could run it. Customizing starts a copy
  // under a new name instead.
  const customize = () => {
    if (!draft) return;
    const copy: FlowDraft = {
      ...draft,
      name: `${draft.name}-custom`,
      status: "candidate",
      steps: draft.steps.map((step) => ({
        ...step,
        inputs: { ...step.inputs },
      })),
    };
    closeEditor();
    startCreating(copy);
    setMessage(
      "This is a copy of a built-in flow. The built-in stays as shipped.",
    );
  };
  const selectedFlow = flows.data?.find((flow) => flow.name === selected);
  const readOnly = selectedFlow?.source === "builtin";
  const actions = flowActions(
    selectedFlow?.status || "",
    selectedFlow?.source || "",
    testedRun,
  );
  const editable = creating || (Boolean(selectedFlow) && !readOnly);
  const canSave = Boolean(
    draft?.name.trim() &&
    draft?.description.trim() &&
    draft?.steps.length &&
    flowStepsComplete(draft.steps) &&
    editable,
  );
  const flowEditor = draft ? (
    <section className="flow-builder">
      <div className="flow-builder-intro">
        <div>
          <div className="editor-label">Flow details</div>
          <p>
            {readOnly
              ? usesSystemAction(draft.steps)
                ? "This flow is built into north and runs north's own maintenance, so it is shown as shipped."
                : "This flow is built into north, so it is shown as shipped. Customize makes an editable copy."
              : "Arrange existing skills in order, or write inline instructions when a step does not need a reusable skill. Approval is inherited from the selected skill and system policy."}
          </p>
        </div>
        <span className="flow-source-badge">
          {creating ? "new flow" : selectedFlow?.source}
        </span>
      </div>
      <div className="flow-meta-grid">
        <label>
          Flow name
          <input
            disabled={!creating}
            value={draft.name}
            placeholder="job-application-review"
            onChange={(event) =>
              setDraft({ ...draft, name: event.target.value })
            }
          />
        </label>
        <label>
          Status
          <select
            aria-label="Status"
            value={draft.status}
            disabled={!editable}
            onChange={(event) =>
              setDraft({ ...draft, status: event.target.value })
            }
          >
            <option value="active" disabled>
              Active (tested)
            </option>
            <option value="candidate">Draft / candidate</option>
            <option value="retired">Retired</option>
          </select>
          <small>
            Saving an executable change returns this flow to candidate.
          </small>
        </label>
      </div>
      <label className="flow-wide-field">
        Purpose
        <textarea
          aria-label="Purpose"
          disabled={!editable}
          rows={2}
          value={draft.description}
          placeholder="What should this flow accomplish?"
          onChange={(event) =>
            setDraft({ ...draft, description: event.target.value })
          }
        />
      </label>
      <div className="flow-steps-heading">
        <div>
          <h2>Steps</h2>
          <p>
            North executes each selected skill or inline procedure from top to
            bottom.
          </p>
        </div>
        <button
          className="ghost-button"
          disabled={!editable}
          onClick={() =>
            setDraft({ ...draft, steps: [...draft.steps, emptyStep()] })
          }
        >
          + Add step
        </button>
      </div>
      {draft.steps.map((step, index) => (
        <FlowStepCard
          key={index}
          step={step}
          index={index}
          editable={editable}
          skills={skills.data || []}
          canRemove={editable && draft.steps.length > 1}
          onRemove={() =>
            setDraft({
              ...draft,
              steps: draft.steps.filter((_, stepIndex) => stepIndex !== index),
            })
          }
          onChange={(next) =>
            setDraft({
              ...draft,
              steps: draft.steps.map((current, stepIndex) =>
                stepIndex === index ? next : current,
              ),
            })
          }
        />
      ))}
    </section>
  ) : (
    <Loading />
  );

  const schedules = cron.data || [];
  const byFlow = schedulesByFlow(schedules);
  const allRuns = runs.data || [];
  const upcoming = buildUpcoming(schedules, jobs.data || [], allRuns);
  const registered = flows.data || [];
  const mine = registered.filter((flow) => flow.source !== "builtin");
  const builtins = registered.filter((flow) => flow.source === "builtin");
  const toggleRun = (id: string) => setOpenRun(openRun === id ? "" : id);

  const flowRow = (flow: FlowSummary) => (
    <div className="schedule-row flow-row" key={flow.name}>
      <div className="schedule-main">
        <b>{flow.name}</b>
        <small className="schedule-description">{flow.description}</small>
        <small>
          {flow.steps} step{flow.steps === 1 ? "" : "s"} ·{" "}
          {scheduleSummary(byFlow.get(flow.name) || [])} ·{" "}
          {lastRunText(lastRunOf(allRuns, flow.name))}
        </small>
      </div>
      <div className="schedule-actions">
        {flow.source === "builtin" && (
          <span className="schedule-locked">built-in</span>
        )}
        <Status value={flow.status} />
        <button
          className="ghost-button"
          aria-haspopup="dialog"
          onClick={() => void open(flow.name)}
        >
          {flow.source === "builtin" ? "Details" : "Open"}
        </button>
      </div>
    </div>
  );

  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Automation"
        title="Flows"
        subtitle="What north runs, when it runs next, and how it went."
        actions={
          <button className="primary-button" onClick={() => startCreating()}>
            + New flow
          </button>
        }
      />
      {(flows.error || cron.error || runs.error) && (
        <ErrorNotice message={flows.error || cron.error || runs.error} />
      )}
      {message && !selected && <div className="notice">{message}</div>}
      {creating && (
        <section className="capability-create-panel flow-create-panel">
          <header>
            <div>
              <span>New automation</span>
              <h2>Create flow</h2>
              <p>
                Compose skills and inline procedures into an ordered process.
              </p>
            </div>
            <div>
              <button
                className="ghost-button"
                onClick={() => {
                  setCreating(false);
                  setDraft(null);
                }}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                disabled={!canSave}
                onClick={() => void create()}
              >
                Create flow
              </button>
            </div>
          </header>
          {flowEditor}
        </section>
      )}

      <Panel title="Next up" label="soonest first">
        {flows.loading || cron.loading || jobs.loading ? (
          <Loading />
        ) : upcoming.length ? (
          upcoming.slice(0, 8).map((item) => (
            <div className="schedule-row upcoming" key={item.key}>
              <div className="schedule-main">
                <b>{item.title}</b>
                <small title={item.absolute}>
                  {item.epoch
                    ? `${whenFromNow(item.epoch)} · ${item.absolute}`
                    : item.absolute}{" "}
                  · {item.kind}
                  {item.title !== item.flow && ` · ${item.flow}`}
                </small>
              </div>
            </div>
          ))
        ) : (
          <Empty>
            Nothing is queued. Schedule a flow, or ask north to run one.
          </Empty>
        )}
      </Panel>

      <Panel title="Your flows" label={`${mine.length} custom`}>
        {flows.loading ? (
          <Loading />
        ) : mine.length ? (
          mine.map(flowRow)
        ) : (
          <Empty>
            No flows of your own yet. Create one above, or ask north to build
            one.
          </Empty>
        )}
      </Panel>

      <Panel title="Built into north" label="read-only">
        {flows.loading ? (
          <Loading />
        ) : builtins.length ? (
          builtins.map(flowRow)
        ) : (
          <Empty>North has no built-in flows.</Empty>
        )}
      </Panel>

      <Panel title="Recent runs" label="history">
        {runs.loading ? (
          <Loading />
        ) : allRuns.length ? (
          allRuns
            .slice(0, 8)
            .map((run) => (
              <RunRow
                key={run.run_id}
                run={run}
                showFlow
                open={openRun === run.run_id}
                onToggle={() => toggleRun(run.run_id)}
              />
            ))
        ) : (
          <Empty>No flow has run yet.</Empty>
        )}
      </Panel>

      <CapabilityModal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Flow"}
        eyebrow={selectedFlow?.source || "Automation"}
        className="flow-modal"
        actions={
          <>
            {actions.test && (
              <button
                className="ghost-button"
                onClick={() => void startRun("test")}
              >
                Test run
              </button>
            )}
            {actions.run && (
              <button
                className="ghost-button"
                onClick={() => void startRun("execute")}
              >
                Run now
              </button>
            )}
            {actions.activate && (
              <button
                className="primary-button"
                onClick={() => void activate()}
              >
                Activate
              </button>
            )}
            {readOnly && !usesSystemAction(draft?.steps || []) && (
              <button className="primary-button" onClick={customize}>
                Customize
              </button>
            )}
            {selectedFlow?.source === "learned" && (
              <button className="danger-button" onClick={() => void remove()}>
                Delete flow
              </button>
            )}
            {!readOnly && tab === "definition" && (
              <button
                className="primary-button"
                disabled={!canSave}
                onClick={() => void save()}
              >
                Save flow
              </button>
            )}
          </>
        }
      >
        {message && <div className="notice flow-modal-notice">{message}</div>}
        <div className="segmented flow-tabs">
          {(
            [
              ["definition", "Definition"],
              ["schedule", "Schedule"],
              ["runs", "Runs"],
            ] as [FlowTab, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={tab === value ? "active" : ""}
              onClick={() => setTab(value)}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === "definition" && flowEditor}
        {tab === "schedule" && (
          <div className="flow-tab-body">
            <FlowSchedules
              flow={selected || ""}
              entries={byFlow.get(selected || "") || []}
              oneOffs={(jobs.data || []).filter(
                (job) =>
                  job.flow === selected &&
                  !job.cron_entry &&
                  job.status === "queued",
              )}
              canSchedule={actions.schedule}
              onChanged={reloadSchedules}
            />
          </div>
        )}
        {tab === "runs" && (
          <div className="flow-tab-body">
            {history.loading ? (
              <Loading />
            ) : history.data?.length ? (
              history.data.map((run) => (
                <RunRow
                  key={run.run_id}
                  run={run}
                  showFlow={false}
                  open={openRun === run.run_id}
                  onToggle={() => toggleRun(run.run_id)}
                />
              ))
            ) : (
              <Empty>This flow has not run yet.</Empty>
            )}
          </div>
        )}
      </CapabilityModal>
    </div>
  );
}
