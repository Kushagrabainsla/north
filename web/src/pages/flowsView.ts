// Pure view logic for the Flows page. Kept out of the component so it can be
// tested without rendering: the project has no component-test setup.
import { agoText, hhmm, whenFromNow } from "../schedule";
import type { Draft } from "../scheduleForm";

export interface StepLike {
  name: string;
  skill: string;
  instructions: string;
  // A built-in flow's step can be one of north's own maintenance jobs.
  action?: string;
}

// A step is one kind or the other. Naming a skill is a complete procedure on
// its own - the skill body is the instructions - so only a step with no skill
// needs text of its own. The backend parser and validator apply the same rule.
export const flowStepsComplete = (steps: StepLike[]): boolean =>
  steps.every(
    (step) =>
      step.name.trim() &&
      (step.skill.trim() || step.instructions.trim() || step.action),
  );

// Server code can only be named by a built-in flow, so a copy of one that uses
// it could not be saved: such a flow is shown, run and scheduled, never customized.
export const usesSystemAction = (steps: StepLike[]): boolean =>
  steps.some((step) => Boolean(step.action));

// What the schedule API says about a routine, as far as this page reads it.
export interface ScheduleLike {
  name: string;
  title: string;
  flow?: string;
  cadence: string;
  interval_minutes?: number | null;
  hour: number;
  minute: number;
  enabled: boolean;
  next_run_epoch: number;
  next_run_local: string;
}

export interface JobLike {
  job_id: string;
  status: string;
  scheduled_epoch: number;
  scheduled_local: string;
  cron_entry?: string | null;
  flow?: string;
  label?: string;
}

export interface RunLike {
  run_id: string;
  flow: string;
  status: string;
  trigger: string;
  task_id: string;
  current_step: number;
  total_steps: number;
  started_at: string;
  updated_at: string;
  error: string;
}

// "daily at 07:00", or "every 5 minutes" - an interval has no time of day.
export const cadenceText = (entry: ScheduleLike): string =>
  entry.interval_minutes
    ? entry.cadence
    : `${entry.cadence} at ${hhmm(entry.hour, entry.minute)}`;

export function schedulesByFlow<T extends ScheduleLike>(
  entries: T[],
): Map<string, T[]> {
  const grouped = new Map<string, T[]>();
  for (const entry of entries) {
    if (!entry.flow) continue;
    grouped.set(entry.flow, [...(grouped.get(entry.flow) || []), entry]);
  }
  return grouped;
}

// One line for a flow's row: when it runs, or that it does not.
export function scheduleSummary(
  entries: ScheduleLike[],
  now: number = Date.now() / 1000,
): string {
  if (!entries.length) return "not scheduled";
  const live = entries.filter((entry) => entry.enabled);
  if (!live.length) return "schedule paused";
  const soonest = [...live].sort(
    (a, b) => a.next_run_epoch - b.next_run_epoch,
  )[0];
  const extra = live.length > 1 ? ` (+${live.length - 1} more)` : "";
  return `${cadenceText(soonest)} · next ${whenFromNow(soonest.next_run_epoch, now)}${extra}`;
}

// The run that says how a flow is doing: the latest real one. A test only stands
// in when the flow has never run for real, as a candidate has not.
export function lastRunOf<T extends RunLike>(
  runs: T[],
  flow: string,
): T | undefined {
  const mine = runs
    .filter((run) => run.flow === flow)
    .sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));
  return mine.find((run) => run.trigger !== "test") ?? mine[0];
}

export function lastRunText(
  run: RunLike | undefined,
  now: number = Date.now(),
): string {
  if (!run) return "never run";
  const when = agoText(run.started_at, now);
  return when ? `${run.status.replaceAll("_", " ")} ${when}` : run.status;
}

export const TRIGGER_LABELS: Record<string, string> = {
  schedule: "scheduled",
  manual: "run by hand",
  test: "test run",
};
export const triggerLabel = (trigger: string): string =>
  TRIGGER_LABELS[trigger] || "";

// How far a run got. A finished run reports its size; one that stopped early
// says where, because "failed" alone does not tell you which step to look at.
export function progressText(run: RunLike): string {
  const total = run.total_steps;
  const noun = total === 1 ? "step" : "steps";
  if (run.status === "completed") return `${total} ${noun}`;
  const at = Math.min(run.current_step + 1, Math.max(total, 1));
  return `stopped at step ${at} of ${total}`;
}

export interface UpcomingItem {
  key: string;
  flow: string;
  title: string;
  epoch: number;
  absolute: string;
  kind: string;
}

const RUNNING_RUN = new Set(["running"]);
const WAITING_RUN = new Set(["paused"]);

// One timeline of what flows will do next: what is running now, what a
// schedule has already queued, one-off runs, and each live schedule's next
// firing - soonest first. A queued job that names a schedule stands for that
// schedule's next run, so the schedule is not listed a second time.
export function buildUpcoming(
  schedules: ScheduleLike[],
  jobs: JobLike[],
  runs: RunLike[],
): UpcomingItem[] {
  const titleOf = (job: JobLike) =>
    job.label ||
    schedules.find((entry) => entry.name === job.cron_entry)?.title ||
    job.flow ||
    "";
  const flowJobs = jobs.filter((job) => job.flow);
  const runningJobs = flowJobs.filter((job) => job.status === "running");
  const queued = flowJobs.filter(
    (job) => job.status === "queued" || job.status === "retrying",
  );
  const claimed = new Set(
    [...runningJobs, ...queued].map((job) => job.cron_entry).filter(Boolean),
  );
  // A scheduled run's task id is its job's id: the job already covers it.
  const jobIds = new Set(runningJobs.map((job) => job.job_id));

  const items: UpcomingItem[] = [
    ...runs
      .filter((run) => RUNNING_RUN.has(run.status) && !jobIds.has(run.task_id))
      .map((run) => ({
        key: run.run_id,
        flow: run.flow,
        title: run.flow,
        epoch: 0,
        absolute: `started ${agoText(run.started_at)}`,
        kind: "running now",
      })),
    ...runs
      .filter((run) => WAITING_RUN.has(run.status))
      .map((run) => ({
        key: run.run_id,
        flow: run.flow,
        title: run.flow,
        epoch: 0,
        absolute: `paused at step ${Math.min(run.current_step + 1, Math.max(run.total_steps, 1))}`,
        kind: "needs you",
      })),
    ...runningJobs.map((job) => ({
      key: job.job_id,
      flow: job.flow as string,
      title: job.flow as string,
      epoch: 0,
      absolute: `started ${job.scheduled_local}`,
      kind: "running now",
    })),
    ...queued.map((job) => ({
      key: job.job_id,
      flow: job.flow as string,
      title: job.cron_entry ? titleOf(job) : (job.flow as string),
      epoch: job.scheduled_epoch,
      absolute: job.scheduled_local,
      kind: job.cron_entry ? job.status : "once",
    })),
    ...schedules
      .filter(
        (entry) => entry.flow && entry.enabled && !claimed.has(entry.name),
      )
      .map((entry) => ({
        key: entry.name,
        flow: entry.flow as string,
        title: entry.title,
        epoch: entry.next_run_epoch,
        absolute: entry.next_run_local,
        kind: entry.cadence,
      })),
  ];
  return items.sort((a, b) => a.epoch - b.epoch);
}

export interface FlowActions {
  test: boolean;
  run: boolean;
  activate: boolean;
  schedule: boolean;
}

// What can be done with a flow right now. A built-in ships tested, so it is
// only ever run or scheduled; a candidate is tested, then activated on that
// evidence; only an active flow may run for real or go on a schedule.
export const flowActions = (
  status: string,
  source: string,
  testedRunId: string,
): FlowActions => ({
  test: source !== "builtin" && (status === "candidate" || status === "active"),
  run: status === "active",
  activate:
    source !== "builtin" && status === "candidate" && Boolean(testedRunId),
  schedule: status === "active",
});

// A schedule form as the flow schedule endpoints take it. There is no prompt or
// agent: the flow carries the work, and the form only says when.
export function flowScheduleBody(draft: Draft) {
  const label = draft.label.trim();
  if (draft.repeat === "once")
    return { label, run_at: `${draft.date}T${hhmm(draft.hour, draft.minute)}` };
  if (draft.repeat === "interval")
    return { label, interval_minutes: draft.intervalMinutes };
  return {
    label,
    hour: draft.hour,
    minute: draft.minute,
    days: draft.repeat === "custom" ? draft.days : draft.repeat,
  };
}
