import { Fragment, useEffect, useState } from "react";
import { X } from "lucide-react";
import { api, del, patch, post } from "../api";
import { formatDateTime, Modal, PageHeader, Panel } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import {
  ActionBar,
  Button,
  confirmDelete,
  Empty,
  EntityForm,
  EntityTable,
  Field,
  ListRow,
  Loading,
  Notice,
  ResourceView,
  SectionHeader,
  Status,
  Tabs,
} from "../kit";
import { agoText, whenFromNow } from "../schedule";
import { draftOf, emptyDraft, ScheduleForm } from "../scheduleForm";
import type { Draft } from "../scheduleForm";
import {
  buildUpcoming,
  cadenceText,
  flowActions,
  flowDeletionPrompt,
  flowScheduleBody,
  flowStepsComplete,
  inactiveSkillLabel,
  lastRunOf,
  lastRunText,
  progressText,
  scheduleSummary,
  schedulesByFlow,
  stoppedStepText,
  triggerLabel,
  usesSystemAction,
} from "./flowsView";
import type { JobLike, RunLike, ScheduleLike } from "./flowsView";

import {
  CapabilityName,
  capabilityColumns,
  failure,
  sourceLabel,
  type PageMessage,
  type SkillSummary,
} from "./capabilities";

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
  changed_since_activation?: boolean;
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

const APPROVAL_TEXT: Record<string, string> = {
  before_step: "once, before the step",
  on_mutation: "before each change",
  never: "none",
};

// One step of the flow form: its fields sit in the form's own grid, under a
// header that names the step and what it runs.
function FlowStepFields({
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
  // What the step may do, said once in its header.
  const about = [
    step.action
      ? `Runs north's own job: ${step.action}. No model; it is plain code.`
      : "",
    execution
      ? `Allowed tools: ${execution.tools.length ? execution.tools.join(", ") : "none"}. Approval: ${APPROVAL_TEXT[execution.approval] || "none"}.`
      : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <>
      <Field label={`Step ${index + 1}`} wide>
        <SectionHeader
          level={3}
          title={step.name || "Untitled step"}
          description={about || undefined}
          actions={
            <Button
              variant="quiet"
              size="sm"
              disabled={!canRemove}
              onClick={onRemove}
            >
              Remove step
            </Button>
          }
        />
      </Field>
      {!step.action && (
        <>
          <Field label="Step name">
            <input
              disabled={!editable}
              value={step.name}
              placeholder="Review matching jobs"
              onChange={(event) => set({ name: event.target.value })}
            />
          </Field>
          <Field
            label="Kind"
            hint={
              activeSkills.length ? undefined : "No active skills to choose from."
            }
          >
            <select
              aria-label="Kind"
              disabled={!editable}
              value={kind}
              onChange={(event) =>
                event.target.value === "skill"
                  ? chooseSkillKind()
                  : chooseInstructionsKind()
              }
            >
              <option value="skill" disabled={!activeSkills.length}>
                Existing skill
              </option>
              <option value="instructions">Instructions</option>
            </select>
          </Field>
          {kind === "skill" ? (
            <Field
              label="Skill"
              wide
              hint="North runs this step using the selected reusable skill."
            >
              <select
                aria-label="Skill"
                disabled={!editable}
                value={step.skill}
                onChange={(event) => selectSkill(event.target.value)}
              >
                {step.skill &&
                  !activeSkills.some((skill) => skill.name === step.skill) && (
                    <option value={step.skill}>
                      {step.skill} ({inactiveSkillLabel(selectedSkill?.status)})
                    </option>
                  )}
                {activeSkills.map((skill) => (
                  <option key={skill.name} value={skill.name}>
                    {skill.name}
                    {skill.execution ? "" : " · instruction only"}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <Field
              label="Step instructions"
              wide
              hint="North follows these instructions directly - no reusable skill involved."
            >
              <textarea
                aria-label="Step instructions"
                disabled={!editable}
                value={step.instructions}
                rows={3}
                placeholder="Write the procedure North should follow for this step."
                onChange={(event) => set({ instructions: event.target.value })}
              />
            </Field>
          )}
        </>
      )}
      {execution && (
        <>
          <Field
            label="Skill inputs"
            wide
            hint={
              inputs.length ? (
                <>
                  Required fields come from the skill. Pass on an earlier
                  step's result with {"${steps.review.output}"}.
                </>
              ) : (
                "No skill inputs needed."
              )
            }
          >
            <ActionBar align="start">
              <Button
                size="sm"
                disabled={!editable}
                onClick={() => set({ inputs: { ...step.inputs, "": "" } })}
              >
                + Add input
              </Button>
            </ActionBar>
          </Field>
          {inputs.map(([key, value], inputIndex) => {
            const property = execution.inputs.properties?.[key];
            return (
              <Fragment key={`${key}-${inputIndex}`}>
                <Field label="Input name">
                  <input
                    disabled={!editable}
                    aria-label="Input name"
                    placeholder="input name"
                    value={key}
                    onChange={(event) =>
                      setInput(key, event.target.value, value)
                    }
                  />
                </Field>
                <Field label="Value">
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
                  <ActionBar align="start">
                    <Button
                      variant="icon-danger"
                      icon={<X size={14} />}
                      disabled={!editable}
                      aria-label="Remove input"
                      title="Remove input"
                      onClick={() => {
                        const next = { ...step.inputs };
                        delete next[key];
                        set({ inputs: next });
                      }}
                    />
                  </ActionBar>
                </Field>
              </Fragment>
            );
          })}
        </>
      )}
    </>
  );
}

interface FlowRunStep {
  step: string;
  skill: string;
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

// What each step of a run did, and where it stopped, as rows under the run.
function RunDetails({ run }: { run: FlowRunView }) {
  return (
    <>
      {run.steps.map((step) => (
        <ListRow
          key={step.step}
          title={step.step}
          description={step.summary || step.output || stepFallback(step)}
          meta={[
            step.artifacts.length > 0 &&
              `Wrote ${step.artifacts.map((file) => `${file.kind}/${file.name}`).join(", ")}`,
            step.tools_used.length > 0 && `Used ${step.tools_used.join(", ")}`,
          ]}
        />
      ))}
      {run.stopped_at ? (
        <ListRow
          title={`${run.stopped_at.step} · ${run.stopped_at.runs}`}
          description={stoppedStepText(run, formatDateTime)}
        />
      ) : (
        !run.steps.length && (
          <ListRow
            title="Steps"
            description="No step finished before this run stopped."
          />
        )
      )}
      {run.task_id && (
        <ListRow
          title="Task"
          description="Open its timeline"
          to={`/tasks/${run.task_id}`}
        />
      )}
    </>
  );
}

// One run's row, opening to what each step did. Used under "Recent runs" and in
// a flow's own history, so a run reads the same wherever it is listed. A paused
// run - one that stopped on an error or a restart - can be resumed at the step
// it stopped on, or discarded (#33).
function RunRow({
  run,
  showFlow,
  open,
  onToggle,
  onChanged,
}: {
  run: FlowRunView;
  showFlow: boolean;
  open: boolean;
  onToggle: () => void;
  onChanged: () => Promise<unknown>;
}) {
  const dialog = useDialog();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const act = async (action: "resume" | "discard") => {
    if (
      action === "discard" &&
      !(await confirmDelete(dialog, {
        verb: "Discard",
        noun: "run",
        name: run.flow,
        consequence: `This run is ${run.status}. The steps it finished stay on record; the rest will not run.`,
      }))
    )
      return;
    setBusy(true);
    setActionError("");
    try {
      await post(
        `/web/api/flow-runs/${encodeURIComponent(run.run_id)}/${action}`,
        {},
      );
      await onChanged();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const how = triggerLabel(run.trigger);
  const stopped = run.status === "paused" || run.status === "waiting";
  const hasBody = Boolean(run.error || actionError || open);
  return (
    <ListRow
      title={showFlow ? run.flow : how || "run"}
      meta={[
        showFlow && how,
        <span title={formatDateTime(run.started_at)}>
          {agoText(run.started_at)}
        </span>,
        run.late_note,
        progressText(run),
      ]}
      status={<Status value={run.status} />}
      actions={
        <ActionBar
          danger={
            stopped && (
              <Button
                variant="danger"
                size="sm"
                disabled={busy}
                onClick={() => void act("discard")}
              >
                Discard
              </Button>
            )
          }
        >
          {stopped && (
            <Button
              variant="quiet"
              size="sm"
              disabled={busy}
              onClick={() => void act("resume")}
              title={
                run.status === "waiting"
                  ? "Try now, instead of waiting for north to retry it"
                  : "Run it again from the step it stopped on"
              }
            >
              Resume
            </Button>
          )}
          <Button variant="quiet" size="sm" onClick={onToggle}>
            {open ? "Hide" : "Details"}
          </Button>
        </ActionBar>
      }
    >
      {hasBody && (
        <>
          {run.error && <Notice tone="error">{run.error}</Notice>}
          {actionError && <Notice tone="error">{actionError}</Notice>}
          {open && <RunDetails run={run} />}
        </>
      )}
    </ListRow>
  );
}

// A flow's schedules, managed where the flow is: when it runs is a property of
// the flow, so it is added, retimed, paused and removed here. There is no prompt
// and nothing else to choose - the flow carries the work.
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
  const dialog = useDialog();
  const [editing, setEditing] = useState("");
  const [creating, setCreating] = useState(false);
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
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const close = () => {
    setEditing("");
    setCreating(false);
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
  const removeEntry = async (entry: ScheduleView) => {
    if (
      await confirmDelete(dialog, {
        noun: "schedule",
        name: entry.title,
        consequence: "The flow stays; it just stops running on this schedule.",
      })
    )
      await act(() => del(path(entry.name)));
  };
  const removeOnce = async (job: JobLike) => {
    if (
      await confirmDelete(dialog, {
        noun: "one-time run",
        name: job.scheduled_local,
        consequence: "It will not run. The flow stays.",
      })
    )
      await act(() =>
        del(`/orchestrator/jobs/${encodeURIComponent(job.job_id)}`),
      );
  };

  return (
    <>
      <SectionHeader
        level={3}
        title="Schedules"
        description={
          canSchedule
            ? "When this flow runs. Each schedule runs the whole flow."
            : "Test and activate this flow before scheduling it."
        }
        actions={
          <Button onClick={startCreate} disabled={!canSchedule || busy}>
            + New schedule
          </Button>
        }
      />
      {creating && (
        <ScheduleForm
          title="New schedule"
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
        <ListRow
          key={job.job_id}
          title="Once"
          meta={[job.scheduled_local]}
          actions={
            <ActionBar
              danger={
                <Button
                  variant="danger"
                  size="sm"
                  disabled={busy}
                  onClick={() => void removeOnce(job)}
                >
                  Delete
                </Button>
              }
            />
          }
        />
      ))}
      {entries.map((entry) => (
        <ListRow
          key={entry.name}
          title={entry.title}
          muted={!entry.enabled}
          meta={[
            cadenceText(entry),
            entry.enabled ? `next ${entry.next_run_local}` : "paused",
          ]}
          actions={
            <ActionBar
              danger={
                <Button
                  variant="danger"
                  size="sm"
                  disabled={busy}
                  onClick={() => void removeEntry(entry)}
                >
                  Delete
                </Button>
              }
            >
              <Button
                variant="quiet"
                size="sm"
                disabled={busy}
                onClick={() =>
                  act(() =>
                    patch(path(entry.name), { enabled: !entry.enabled }),
                  )
                }
              >
                {entry.enabled ? "Pause" : "Resume"}
              </Button>
              <Button
                variant="quiet"
                size="sm"
                disabled={busy}
                onClick={() => startEdit(entry)}
              >
                Edit
              </Button>
            </ActionBar>
          }
        >
          {editing === entry.name && (
            <ScheduleForm
              title="Change schedule"
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
        </ListRow>
      ))}
      {error && !creating && !editing && (
        <Notice tone="error">{error}</Notice>
      )}
    </>
  );
}

type FlowTab = "definition" | "schedule" | "runs";

const FLOW_TABS: { value: FlowTab; label: string }[] = [
  { value: "definition", label: "Definition" },
  { value: "schedule", label: "Schedule" },
  { value: "runs", label: "Runs" },
];

const FLOW_COLUMNS = capabilityColumns("Flow", "Schedule");

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
  const [message, setMessage] = useState<PageMessage | null>(null);
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
  const [changedSinceActivation, setChangedSinceActivation] = useState(false);
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
        if (!current) return;
        setTestedRun(detail.tested_run_id || "");
        setChangedSinceActivation(Boolean(detail.changed_since_activation));
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
    setMessage(null);
  };
  const closeEditor = () => {
    setSelected(null);
    setDraft(null);
  };
  const open = async (name: string) => {
    setSelected(name);
    setCreating(false);
    setDraft(null);
    setMessage(null);
    setTab("definition");
    try {
      const detail = await api<FlowDetail>(
        `/web/api/flow-definitions/${encodeURIComponent(name)}`,
      );
      setDraft(draftFromDetail(detail));
      setTestedRun(detail.tested_run_id || "");
      setChangedSinceActivation(Boolean(detail.changed_since_activation));
    } catch (error) {
      setSelected(null);
      setMessage(failure(error));
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
      setMessage({
        tone: "success",
        text:
          mode === "test"
            ? "Test run started. It does the real work once, on a small case. Anything that needs approval is under Approvals."
            : "Run started. Anything that needs approval is under Approvals.",
      });
      await Promise.all([history.reload(), runs.reload()]);
    } catch (error) {
      setMessage(failure(error));
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
      setChangedSinceActivation(false);
      setMessage({
        tone: "success",
        text: "Flow activated. It can now be scheduled and run.",
      });
      await flows.reload();
    } catch (error) {
      setMessage(failure(error));
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
      setMessage({
        tone: "success",
        text: "Flow candidate created. Test it before activation.",
      });
      await flows.reload();
    } catch (error) {
      setMessage(failure(error));
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
      setChangedSinceActivation(Boolean(detail.changed_since_activation));
      setMessage({
        tone: "success",
        text: "Flow saved as a candidate. Test it before activation.",
      });
      await flows.reload();
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      flows.data?.find((flow) => flow.name === selected)?.source !==
        "learned" ||
      !(await confirmDelete(dialog, {
        noun: "flow",
        name: selected,
        consequence: flowDeletionPrompt(
          selected,
          (byFlow.get(selected) || []).map((entry) => entry.title),
          (jobs.data || []).filter(
            (job) =>
              job.flow === selected &&
              !job.cron_entry &&
              job.status === "queued",
          ).length,
        ),
      }))
    )
      return;
    try {
      await del(`/web/api/flow-definitions/${encodeURIComponent(selected)}`);
      setSelected(null);
      setDraft(null);
      setMessage({ tone: "success", text: "Learned flow deleted." });
      await flows.reload();
    } catch (error) {
      setMessage(failure(error));
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
    setMessage({
      tone: "info",
      text: "This is a copy of a built-in flow. The built-in stays as shipped.",
    });
  };
  const cancelCreating = () => {
    setCreating(false);
    setDraft(null);
  };
  const selectedFlow = flows.data?.find((flow) => flow.name === selected);
  const readOnly = selectedFlow?.source === "builtin";
  const systemFlow = usesSystemAction(draft?.steps || []);
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
  // One form for creating, changing and reading a flow. A built-in is read
  // only: its one action is to copy it (Customize), unless it runs north's own
  // upkeep, which cannot be copied.
  const flowForm = draft ? (
    <EntityForm
      title={creating ? "Create flow" : "Flow details"}
      label={creating ? "New automation" : sourceLabel(selectedFlow?.source || "")}
      description={
        readOnly
          ? systemFlow
            ? "This flow is built into north and runs north's own maintenance, so it is shown as shipped."
            : "This flow is built into north, so it is shown as shipped. Customize makes an editable copy."
          : "Arrange existing skills in order, or write inline instructions when a step does not need a reusable skill. Approval is inherited from the selected skill and system policy."
      }
      submitLabel={creating ? "Create flow" : readOnly ? "Customize" : "Save flow"}
      canSubmit={readOnly ? !systemFlow : canSave}
      onSubmit={creating ? create : readOnly ? customize : save}
      onCancel={creating ? cancelCreating : undefined}
    >
      <Field label="Flow name">
        <input
          disabled={!creating}
          value={draft.name}
          placeholder="job-application-review"
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
      </Field>
      <Field
        label="Status"
        hint="Saving an executable change returns this flow to candidate."
      >
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
      </Field>
      <Field label="Purpose" wide>
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
      </Field>
      <Field
        label="Steps"
        wide
        hint="North runs each selected skill or inline procedure from top to bottom."
      >
        <ActionBar align="start">
          <Button
            size="sm"
            disabled={!editable}
            onClick={() =>
              setDraft({ ...draft, steps: [...draft.steps, emptyStep()] })
            }
          >
            + Add step
          </Button>
        </ActionBar>
      </Field>
      {draft.steps.map((step, index) => (
        <FlowStepFields
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
    </EntityForm>
  ) : (
    <Loading />
  );

  const schedules = cron.data || [];
  const byFlow = schedulesByFlow(schedules);
  const allRuns = runs.data || [];
  const upcoming = buildUpcoming(schedules, jobs.data || [], allRuns);
  // Your own flows first, then the ones north ships with.
  const ordered = (rows: FlowSummary[]) => [
    ...rows.filter((flow) => flow.source !== "builtin"),
    ...rows.filter((flow) => flow.source === "builtin"),
  ];
  const toggleRun = (id: string) => setOpenRun(openRun === id ? "" : id);
  const reloadRuns = () => Promise.all([runs.reload(), history.reload()]);
  // A list that failed with nothing to show says so in its own place; a failed
  // refresh of a list already shown, or of the schedules, says it here.
  const pageError =
    cron.error ||
    (flows.data ? flows.error : "") ||
    (runs.data ? runs.error : "");

  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Automation"
        title="Flows"
        subtitle="What north runs, when it runs next, and how it went."
        actions={
          <Button variant="primary" onClick={() => startCreating()}>
            + New flow
          </Button>
        }
      />
      {pageError && <Notice tone="error">{pageError}</Notice>}
      {message && !selected && (
        <Notice tone={message.tone}>{message.text}</Notice>
      )}
      {creating && flowForm}

      <ResourceView resource={flows}>
        {(rows) => (
          <EntityTable
            label="Flows"
            columns={FLOW_COLUMNS}
            rows={ordered(rows)}
            rowKey={(flow) => flow.name}
            onOpen={(flow) => void open(flow.name)}
            empty="No flows yet. Create one, or ask north to build one."
            cell={(flow, key) =>
              key === "name" ? (
                <CapabilityName name={flow.name} source={flow.source} />
              ) : key === "description" ? (
                flow.description
              ) : key === "fact" ? (
                <>
                  {flow.steps} step{flow.steps === 1 ? "" : "s"} ·{" "}
                  {scheduleSummary(byFlow.get(flow.name) || [])}
                  <small>{lastRunText(lastRunOf(allRuns, flow.name))}</small>
                </>
              ) : (
                <Status value={flow.status} />
              )
            }
          />
        )}
      </ResourceView>

      <Panel title="Next up" label="soonest first">
        {flows.loading || cron.loading || jobs.loading ? (
          <Loading />
        ) : upcoming.length ? (
          upcoming.slice(0, 8).map((item) => (
            <ListRow
              key={item.key}
              title={item.title}
              meta={[
                <span title={item.absolute}>
                  {item.epoch
                    ? `${whenFromNow(item.epoch)} · ${item.absolute}`
                    : item.absolute}
                </span>,
                item.kind,
                item.title !== item.flow && item.flow,
              ]}
            />
          ))
        ) : (
          <Empty>
            Nothing is queued. Schedule a flow, or ask north to run one.
          </Empty>
        )}
      </Panel>

      <Panel title="Recent runs" label="history">
        <ResourceView
          resource={runs}
          isEmpty={(rows) => !rows.length}
          empty="No flow has run yet."
        >
          {(rows) =>
            rows
              .slice(0, 8)
              .map((run) => (
                <RunRow
                  key={run.run_id}
                  run={run}
                  showFlow
                  open={openRun === run.run_id}
                  onToggle={() => toggleRun(run.run_id)}
                  onChanged={reloadRuns}
                />
              ))
          }
        </ResourceView>
      </Panel>

      <Modal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Flow"}
        eyebrow={selectedFlow ? sourceLabel(selectedFlow.source) : "Automation"}
        className="flow-modal"
        actions={
          <>
            {selectedFlow?.source === "learned" && (
              <Button variant="danger" onClick={() => void remove()}>
                Delete
              </Button>
            )}
            {actions.test && (
              <Button onClick={() => void startRun("test")}>Test run</Button>
            )}
            {actions.run && (
              <Button onClick={() => void startRun("execute")}>Run now</Button>
            )}
            {actions.activate && (
              // The Definition tab's form has its own primary (Save), and a
              // dialog shows one primary at a time.
              <Button
                variant={tab === "definition" ? "secondary" : "primary"}
                onClick={() => void activate()}
              >
                Activate
              </Button>
            )}
          </>
        }
      >
        {message && (
          <div className="flow-modal-notice">
            <Notice tone={message.tone}>{message.text}</Notice>
          </div>
        )}
        {changedSinceActivation && (
          <div className="flow-modal-notice">
            <Notice tone="warning">
              This flow changed after it was activated, so its schedule will
              not run it. Test it, then activate it again.
            </Notice>
          </div>
        )}
        <div className="flow-tabs">
          <Tabs
            label="Flow sections"
            items={FLOW_TABS}
            value={tab}
            onChange={setTab}
          />
        </div>
        <div className="flow-tab-body">
          {tab === "definition" && flowForm}
          {tab === "schedule" && (
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
          )}
          {tab === "runs" && (
            <ResourceView
              resource={history}
              isEmpty={(rows) => !rows.length}
              empty="This flow has not run yet."
            >
              {(rows) =>
                rows.map((run) => (
                  <RunRow
                    key={run.run_id}
                    run={run}
                    showFlow={false}
                    open={openRun === run.run_id}
                    onToggle={() => toggleRun(run.run_id)}
                    onChanged={reloadRuns}
                  />
                ))
              }
            </ResourceView>
          )}
        </div>
      </Modal>
    </div>
  );
}
