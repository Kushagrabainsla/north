import { describe, expect, it } from "vitest";
import {
  buildUpcoming,
  cadenceText,
  flowActions,
  flowDeletionPrompt,
  flowScheduleBody,
  flowStepsComplete,
  usesSystemAction,
  lastRunOf,
  lastRunText,
  progressText,
  scheduleSummary,
  schedulesByFlow,
  triggerLabel,
} from "./flowsView";
import type { JobLike, RunLike, ScheduleLike } from "./flowsView";
import type { Draft } from "../scheduleForm";

const NOW = 1_800_000_000; // seconds

const schedule = (patch: Partial<ScheduleLike> = {}): ScheduleLike => ({
  name: "news_daily_briefing",
  title: "Daily news briefing",
  flow: "daily-news-briefing",
  cadence: "daily",
  hour: 7,
  minute: 0,
  enabled: true,
  next_run_epoch: NOW + 3 * 3600,
  next_run_local: "2026-09-25 07:00 PDT",
  ...patch,
});

const job = (patch: Partial<JobLike> = {}): JobLike => ({
  job_id: "j1",
  status: "queued",
  scheduled_epoch: NOW + 600,
  scheduled_local: "2026-09-24 08:00 PDT",
  cron_entry: "news_daily_briefing",
  flow: "daily-news-briefing",
  ...patch,
});

const run = (patch: Partial<RunLike> = {}): RunLike => ({
  run_id: "r1",
  flow: "daily-news-briefing",
  status: "completed",
  trigger: "schedule",
  task_id: "j0",
  current_step: 1,
  total_steps: 1,
  started_at: new Date((NOW - 7200) * 1000).toISOString(),
  updated_at: new Date((NOW - 7100) * 1000).toISOString(),
  error: "",
  ...patch,
});

describe("flowStepsComplete", () => {
  it("accepts a skill step that has no instructions of its own", () => {
    expect(
      flowStepsComplete([
        { name: "brief", skill: "news-briefing", instructions: "" },
      ]),
    ).toBe(true);
  });

  it("accepts an instruction step that has no skill", () => {
    expect(
      flowStepsComplete([
        { name: "remind", skill: "", instructions: "Say hello." },
      ]),
    ).toBe(true);
  });

  it("rejects a step that has neither a skill nor instructions", () => {
    expect(
      flowStepsComplete([{ name: "empty", skill: "", instructions: "  " }]),
    ).toBe(false);
  });

  it("rejects a step with no name even when it has a skill", () => {
    expect(
      flowStepsComplete([
        { name: " ", skill: "news-briefing", instructions: "" },
      ]),
    ).toBe(false);
  });

  it("requires every step to be complete", () => {
    expect(
      flowStepsComplete([
        { name: "one", skill: "news-briefing", instructions: "" },
        { name: "two", skill: "", instructions: "" },
      ]),
    ).toBe(false);
  });
});

describe("system actions", () => {
  it("counts a built-in step that names a system action as complete", () => {
    expect(
      flowStepsComplete([
        {
          name: "clean-up",
          skill: "",
          instructions: "",
          action: "task_context_cleanup",
        },
      ]),
    ).toBe(true);
  });

  it("knows which flows cannot be copied", () => {
    expect(
      usesSystemAction([{ name: "a", skill: "s", instructions: "" }]),
    ).toBe(false);
    expect(
      usesSystemAction([
        { name: "a", skill: "", instructions: "", action: "x" },
      ]),
    ).toBe(true);
  });
});

describe("schedule wording", () => {
  it("gives a wall-clock schedule its time and an interval none", () => {
    expect(cadenceText(schedule())).toBe("daily at 07:00");
    expect(
      cadenceText(
        schedule({ interval_minutes: 5, cadence: "every 5 minutes" }),
      ),
    ).toBe("every 5 minutes");
  });

  it("says a flow with no schedule is not scheduled", () => {
    expect(scheduleSummary([], NOW)).toBe("not scheduled");
  });

  it("says when the next run is", () => {
    expect(scheduleSummary([schedule()], NOW)).toBe(
      "daily at 07:00 · next in 3 h",
    );
  });

  it("says a flow whose schedules are all paused is paused", () => {
    expect(scheduleSummary([schedule({ enabled: false })], NOW)).toBe(
      "schedule paused",
    );
  });

  it("reports the soonest of several schedules and how many more there are", () => {
    const later = schedule({ name: "b", next_run_epoch: NOW + 86400 });
    const sooner = schedule({ name: "a", next_run_epoch: NOW + 1800 });
    expect(scheduleSummary([later, sooner], NOW)).toBe(
      "daily at 07:00 · next in 30 min (+1 more)",
    );
  });

  it("groups schedules by the flow they run and ignores prompt-only ones", () => {
    const grouped = schedulesByFlow([
      schedule(),
      schedule({ name: "x", flow: "" }),
      schedule({ name: "y", flow: undefined }),
    ]);
    expect([...grouped.keys()]).toEqual(["daily-news-briefing"]);
  });
});

describe("run history wording", () => {
  it("says a flow that never ran never ran", () => {
    expect(lastRunText(undefined)).toBe("never run");
  });

  it("picks a flow's most recent run and says how long ago it was", () => {
    const older = run({
      run_id: "old",
      started_at: new Date((NOW - 90000) * 1000).toISOString(),
    });
    const newer = run({ run_id: "new" });
    expect(lastRunOf([older, newer], "daily-news-briefing")?.run_id).toBe(
      "new",
    );
    expect(lastRunOf([older, newer], "other")).toBeUndefined();
    expect(lastRunText(newer, NOW * 1000)).toBe("completed 2h ago");
  });

  it("reports the latest real run over a later test, and a test only when nothing else ran", () => {
    const real = run({
      run_id: "real",
      trigger: "schedule",
      started_at: new Date((NOW - 90000) * 1000).toISOString(),
    });
    const test = run({ run_id: "test", trigger: "test" });
    expect(lastRunOf([real, test], "daily-news-briefing")?.run_id).toBe("real");
    expect(lastRunOf([test], "daily-news-briefing")?.run_id).toBe("test");
  });

  it("names how a run started, and nothing for one recorded before that was kept", () => {
    expect(triggerLabel("schedule")).toBe("scheduled");
    expect(triggerLabel("test")).toBe("test run");
    expect(triggerLabel("")).toBe("");
  });

  it("reports the size of a finished run and where an unfinished one stopped", () => {
    expect(progressText(run())).toBe("1 step");
    expect(progressText(run({ total_steps: 3, current_step: 3 }))).toBe(
      "3 steps",
    );
    expect(
      progressText(run({ status: "failed", total_steps: 3, current_step: 1 })),
    ).toBe("stopped at step 2 of 3");
  });
});

describe("buildUpcoming", () => {
  it("lists a live schedule's next firing", () => {
    const items = buildUpcoming([schedule()], [], []);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      flow: "daily-news-briefing",
      title: "Daily news briefing",
      kind: "daily",
    });
  });

  it("does not list a paused schedule or a prompt-only one", () => {
    expect(
      buildUpcoming(
        [schedule({ enabled: false }), schedule({ name: "x", flow: "" })],
        [],
        [],
      ),
    ).toEqual([]);
  });

  it("lists a queued firing once instead of also predicting the same schedule", () => {
    const items = buildUpcoming([schedule()], [job()], []);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      key: "j1",
      title: "Daily news briefing",
      kind: "queued",
    });
  });

  it("shows a one-off run of a flow as once", () => {
    const items = buildUpcoming([], [job({ cron_entry: null })], []);
    expect(items[0]).toMatchObject({
      kind: "once",
      title: "daily-news-briefing",
    });
  });

  it("ignores jobs that do not run a flow", () => {
    expect(buildUpcoming([], [job({ flow: "" })], [])).toEqual([]);
  });

  it("puts what is running or waiting on you first, then soonest first", () => {
    const items = buildUpcoming(
      [schedule({ name: "later", next_run_epoch: NOW + 9000 })],
      [job({ job_id: "q", cron_entry: null, scheduled_epoch: NOW + 100 })],
      [run({ run_id: "waiting", status: "paused", task_id: "t" })],
    );
    expect(items.map((item) => item.key)).toEqual(["waiting", "q", "later"]);
    expect(items[0].kind).toBe("needs you");
  });

  it("does not show a scheduled run twice while its job is running", () => {
    const items = buildUpcoming(
      [],
      [job({ status: "running" })],
      [run({ status: "running", task_id: "j1" })],
    );
    expect(items.map((item) => item.key)).toEqual(["j1"]);
    expect(items[0].kind).toBe("running now");
  });

  it("shows a hand-started run that is still going", () => {
    const items = buildUpcoming(
      [],
      [],
      [run({ status: "running", trigger: "manual", task_id: "" })],
    );
    expect(items[0]).toMatchObject({ key: "r1", kind: "running now" });
  });
});

describe("flowActions", () => {
  it("lets a candidate be tested, and activated only once a test has passed", () => {
    expect(flowActions("candidate", "learned", "")).toEqual({
      test: true,
      run: false,
      activate: false,
      schedule: false,
    });
    expect(flowActions("candidate", "learned", "run-1").activate).toBe(true);
  });

  it("lets an active flow be tested again, run, and scheduled", () => {
    expect(flowActions("active", "learned", "")).toEqual({
      test: true,
      run: true,
      activate: false,
      schedule: true,
    });
  });

  it("only runs or schedules a built-in flow, which ships tested", () => {
    expect(flowActions("active", "builtin", "")).toEqual({
      test: false,
      run: true,
      activate: false,
      schedule: true,
    });
  });

  it("offers nothing on a retired flow", () => {
    expect(flowActions("retired", "learned", "run-1")).toEqual({
      test: false,
      run: false,
      activate: false,
      schedule: false,
    });
  });
});

describe("flowScheduleBody", () => {
  const draft = (patch: Partial<Draft> = {}): Draft => ({
    label: " Morning run ",
    hour: 7,
    minute: 5,
    intervalMinutes: 15,
    repeat: "daily",
    days: [],
    date: "2026-10-01",
    ...patch,
  });

  it("sends a repeating schedule as a time and a rule", () => {
    expect(flowScheduleBody(draft())).toEqual({
      label: "Morning run",
      hour: 7,
      minute: 5,
      days: "daily",
    });
  });

  it("sends the chosen days only for a weekly-on-selected-days schedule", () => {
    expect(
      flowScheduleBody(draft({ repeat: "custom", days: [1, 3] })).days,
    ).toEqual([1, 3]);
    expect(flowScheduleBody(draft({ repeat: "weekdays" })).days).toBe(
      "weekdays",
    );
  });

  it("sends an interval as its length alone", () => {
    expect(flowScheduleBody(draft({ repeat: "interval" }))).toEqual({
      label: "Morning run",
      interval_minutes: 15,
    });
  });

  it("sends a single run as a local date and time", () => {
    expect(flowScheduleBody(draft({ repeat: "once" }))).toEqual({
      label: "Morning run",
      run_at: "2026-10-01T07:05",
    });
  });
});

describe("flowDeletionPrompt", () => {
  it("names every schedule and queued run that goes with the flow", () => {
    const prompt = flowDeletionPrompt(
      "daily-news-briefing",
      ["Daily news briefing", "Evening digest"],
      2,
    );

    expect(prompt).toContain("Delete flow 'daily-news-briefing'?");
    expect(prompt).toContain("• Daily news briefing");
    expect(prompt).toContain("• Evening digest");
    expect(prompt).toContain("2 queued runs will also be cancelled.");
  });

  it("does not imply a cascade when nothing else depends on the flow", () => {
    expect(flowDeletionPrompt("scratch", [], 0)).toBe(
      "Delete flow 'scratch'? This cannot be undone.",
    );
  });
});
