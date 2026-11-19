"""A flow's schedules, from the words a model sends to create_flow to what gets stored.

A schedule is a property of a flow, so create_flow adds (schedule), shows (list,
read), changes (reschedule) and removes (unschedule) them. It was four tools
beside the flow tools: schedule_task, list_schedules, update_schedule and
cancel_schedule.

The case that matters most here is "remind me to stretch every weekday at 9:30".
It reached the tool as a list of days, met `int()`, and came back as
"int() argument must be a string, a bytes-like object or a real number, not
'list'" - an error naming nothing the caller could fix.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from jobs.cron_store import UserCronStore
from jobs.models import REMINDER_FLOW, JobStatus
from tools.models import ToolInput
from tools.universal._schedules import parse_weekdays
from tools.universal.create_flow import CreateFlowTool
from tools.universal.flow_schedules import FlowSchedules
from utils.retired_tools import current_tool


class _Queue:
    """Stands in for the job queue: one-shot schedules become jobs, not cron rows."""

    def __init__(self) -> None:
        self.jobs: list = []

    async def enqueue(self, job) -> None:
        self.jobs.append(job)

    async def list_jobs(self, limit: int = 200, status=None) -> list:
        return [job for job in self.jobs if job.status is JobStatus.PENDING][:limit]

    async def get(self, job_id: str):
        return next((job for job in self.jobs if job.job_id == job_id), None)

    async def cancel(self, job_id: str) -> None:
        job = await self.get(job_id)
        job.status = JobStatus.CANCELLED


def _flows(tmp_path, flows: dict[str, str]):
    """A registry of instruction-only flows, name -> status, whose description is derived from the name."""
    from flows.registry import FlowRegistry

    for name, status in flows.items():
        directory = tmp_path / "flows" / name
        directory.mkdir(parents=True)
        (directory / "FLOW.yaml").write_text(
            f"name: {name}\ndescription: Compile the {name}\nstatus: {status}\n"
            "steps:\n  - name: go\n    instructions: Do it.\n    approval: never\n",
            encoding="utf-8",
        )
    return FlowRegistry(tmp_path / "flows")


@pytest.fixture
def store(tmp_path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


@pytest.fixture
def queue() -> _Queue:
    return _Queue()


def _tool(tmp_path, queue, store, flows: dict[str, str] | None = None, **schedules) -> CreateFlowTool:
    registry = _flows(tmp_path, flows or {})
    checked = {"flow_registry": registry} if flows is not None else {}
    return CreateFlowTool(
        registry,
        learned_dir=tmp_path / "learned",
        schedules=FlowSchedules(queue, store, **checked, **schedules),
    )


@pytest.fixture
def tool(tmp_path, queue, store) -> CreateFlowTool:
    return _tool(tmp_path, queue, store)


async def call(tool, **params):
    return await tool.run(ToolInput(params=params))


async def schedule(tool, **params):
    """A schedule as a model asks for it: every schedule names the flow it runs."""
    return await call(tool, action="schedule", **{"name": "morning-routine", **params})


async def reschedule(tool, **params):
    return await call(tool, action="reschedule", **params)


async def unschedule(tool, **params):
    return await call(tool, action="unschedule", **params)


# ---- one tool for a flow and its schedules ----


def test_the_four_schedule_tools_resolve_to_the_flow_tool() -> None:
    """A skill saved before the fold still names them; each resolves to create_flow."""
    for old in ("schedule_task", "list_schedules", "update_schedule", "cancel_schedule"):
        assert current_tool(old) == "create_flow"


def test_adding_a_schedule_asks_nobody_and_changing_or_removing_one_asks(tool) -> None:
    """As before the fold: schedule_task asked nobody; update and cancel were mutating."""
    assert not tool.mutates({"action": "schedule"})
    assert not tool.mutates({"action": "list"})
    assert tool.mutates({"action": "reschedule"})
    assert tool.mutates({"action": "unschedule"})


def test_the_flow_tool_offers_no_timezone_input() -> None:
    assert "tz" not in CreateFlowTool.parameters_schema["properties"]
    assert "timezone" not in CreateFlowTool.parameters_schema["properties"]


async def test_list_shows_each_flow_with_its_schedules(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store, {"briefing": "active", "quiet": "active"})
    await schedule(tool, name="briefing", hour=7)
    await schedule(tool, name="briefing", run_at="2030-01-01T09:00")

    listed = await call(tool, action="list")

    assert listed.success, listed.error
    by_name = {flow["name"]: flow for flow in listed.data["flows"]}
    assert [view.get("job_id") is not None for view in by_name["briefing"]["schedules"]] == [False, True]
    assert by_name["quiet"]["schedules"] == []
    text = tool.format_output(listed.data)
    assert "briefing - Compile the briefing" in text and "once" in text and "(times in" in text


async def test_read_shows_the_flow_s_own_schedules(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store, {"briefing": "active"})
    await schedule(tool, name="briefing", hour=7, label="Morning briefing")

    read = await call(tool, action="read", name="briefing")

    assert read.success, read.error
    [view] = read.data["schedules"]
    assert view["title"] == "Morning briefing"


# ---- a reminder is a schedule of the reminder flow ----


async def test_a_reminder_is_a_schedule_of_the_reminder_flow(tool, store, queue) -> None:
    repeating = await call(tool, action="schedule", message="Stand up and stretch.", label="Stretch", hour=15)
    once = await call(tool, action="schedule", message="Call mom.", run_at="2030-01-01T09:00")

    assert repeating.success and once.success, (repeating.error, once.error)
    [row] = await store.list()
    assert (row["flow"], row["message"]) == (REMINDER_FLOW, "Stand up and stretch.")
    [job] = queue.jobs
    assert (job.payload["flow"], job.payload["message"], job.payload["label"]) == (
        REMINDER_FLOW,
        "Call mom.",
        "Reminder",
    )


async def test_a_reminder_says_only_where_it_will_appear(tmp_path, queue, store) -> None:
    dashboard_only = _tool(tmp_path / "a", queue, store)
    out = await call(dashboard_only, action="schedule", message="Call mom.", run_at="2030-01-01T09:00")
    assert out.data["delivered_to"] == ["the dashboard"]
    assert dashboard_only.format_output(out.data).endswith("It will appear on the dashboard.")

    telegram = _tool(tmp_path / "b", queue, store, reaches=lambda: ["the dashboard", "Telegram"])
    out = await call(telegram, action="schedule", message="Call mom.", run_at="2030-01-01T09:00")
    assert telegram.format_output(out.data).endswith("It will appear on the dashboard and on Telegram.")


async def test_the_reminder_flow_with_no_message_is_refused(tool, store) -> None:
    """It would fail at every firing: its one step delivers the schedule's text."""
    out = await schedule(tool, name=REMINDER_FLOW, hour=9)

    assert not out.success and "message" in out.error
    assert await store.list() == []


async def test_a_reminder_and_another_flow_at_once_is_refused(tool, queue) -> None:
    out = await schedule(tool, name="call-mom", message="Call mom.", run_at="2030-01-01T09:00")

    assert not out.success
    assert queue.jobs == []


async def test_a_reminder_stored_before_the_fold_becomes_a_reminder_flow_schedule(tmp_path) -> None:
    """Old rows named no flow; the store gives them the reminder flow when it opens."""
    old = UserCronStore(tmp_path / "jobs.db")
    await old.add(name="stretch", agent="north", task="Stretch", hour=9, minute=0, weekdays=None, message="Stretch.")
    await old.add(name="briefing", agent="north", task="Brief", hour=7, minute=0, weekdays=None, flow="briefing")

    reopened = UserCronStore(tmp_path / "jobs.db")

    assert (await reopened.get("stretch"))["flow"] == REMINDER_FLOW
    assert (await reopened.get("briefing"))["flow"] == "briefing"


async def test_a_reminder_queued_before_the_fold_runs_the_reminder_flow(tmp_path) -> None:
    from jobs.models import Job, JobType
    from jobs.sqlite_processor import SQLiteJobProcessor

    queue = SQLiteJobProcessor(tmp_path / "jobs.db")
    await queue.enqueue(
        Job(
            job_id="old",
            type=JobType.ASYNC,
            agent="north",
            task="Laundry",
            payload={"scheduled_by": "schedule_task", "message": "Laundry.", "label": "Laundry"},
            scheduled_at=datetime(2030, 1, 1),
        )
    )

    reopened = SQLiteJobProcessor(tmp_path / "jobs.db")

    assert (await reopened.get("old")).payload["flow"] == REMINDER_FLOW


# ---- reading a day selection ----


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("weekdays", frozenset({0, 1, 2, 3, 4})),
        ("Weekends", frozenset({5, 6})),
        ("daily", None),
        ([0, 1, 2, 3, 4], frozenset({0, 1, 2, 3, 4})),
        (["mon", "thu"], frozenset({0, 3})),
        (["Monday", "Thursday"], frozenset({0, 3})),
        ("tue", frozenset({1})),
        (2, frozenset({2})),
        (["1", "3"], frozenset({1, 3})),
        (None, None),
        ([0, 1, 2, 3, 4, 5, 6], None),
    ],
)
def test_a_day_selection_is_read_however_it_is_said(given, expected) -> None:
    assert parse_weekdays(given) == expected


def test_an_unknown_day_word_says_what_to_send_instead() -> None:
    with pytest.raises(ValueError, match="not a day of the week"):
        parse_weekdays(["mon", "someday"])


def test_the_error_names_days_rather_than_python_types() -> None:
    """The old failure was "int() argument must be ... not 'list'"."""
    with pytest.raises(ValueError) as exc:
        parse_weekdays("often")
    assert "int()" not in str(exc.value)
    assert "weekdays" in str(exc.value)


# ---- adding a schedule ----


async def test_every_weekday_at_930_is_scheduled(tool, store) -> None:
    """The request that used to fail outright."""
    result = await schedule(tool, hour=9, minute=30, days="weekdays")
    assert result.success, result.error
    assert result.data["cadence"] == "weekdays"
    (row,) = await store.list()
    assert row["weekdays"] == frozenset({0, 1, 2, 3, 4})
    assert (row["hour"], row["minute"]) == (9, 30)


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Asia/Kolkata"])
async def test_schedules_use_the_settings_timezone_without_an_input(tool, queue, monkeypatch, zone) -> None:
    from zoneinfo import ZoneInfo

    import utils.time as time_utils

    monkeypatch.setattr(time_utils, "_configured_timezone_name", zone)
    recurring = await schedule(tool, hour=9, minute=30)
    one_shot = await schedule(tool, run_at="2030-01-01T09:30")

    assert recurring.success and recurring.data["tz"] == zone
    assert one_shot.success
    assert queue.jobs[-1].scheduled_at.timestamp() == datetime(2030, 1, 1, 9, 30, tzinfo=ZoneInfo(zone)).timestamp()


@pytest.mark.parametrize("field", ["tz", "timezone"])
async def test_timezone_overrides_are_refused_before_adding_or_changing(tool, store, field) -> None:
    refused = await schedule(tool, hour=9, **{field: "Asia/Kolkata"})
    assert not refused.success and "settings" in refused.error.lower()
    assert await store.list() == []

    created = await schedule(tool, hour=9)
    before = await store.get(created.data["name"])
    refused = await reschedule(tool, schedule=created.data["name"], hour=11, **{field: "Asia/Kolkata"})
    assert not refused.success and "settings" in refused.error.lower()
    assert await store.get(created.data["name"]) == before


async def test_a_bare_list_of_days_is_accepted(tool) -> None:
    """What a model actually sends when asked for "every weekday"."""
    result = await schedule(tool, hour=9, days=[0, 1, 2, 3, 4])
    assert result.success, result.error
    assert result.data["cadence"] == "weekdays"


async def test_omitting_days_schedules_every_day(tool) -> None:
    result = await schedule(tool, hour=15)
    assert result.success and result.data["cadence"] == "daily"


async def test_a_fixed_interval_schedule_is_stored_without_fake_wall_clock_semantics(tool, store) -> None:
    result = await schedule(tool, interval_minutes=5)

    assert result.success, result.error
    assert result.data["cadence"] == "every 5 minutes"
    (row,) = await store.list()
    assert row["interval_minutes"] == 5
    assert row["anchor_epoch"] is not None


async def test_schedule_requires_exactly_one_timing_mode(tool) -> None:
    result = await schedule(tool, hour=9, interval_minutes=5)
    assert not result.success
    assert "exactly one" in result.error


async def test_a_schedule_must_name_the_flow_it_runs(tool, store) -> None:
    result = await call(tool, action="schedule", hour=9, label="Stretch")

    assert not result.success
    assert "message" in result.error and "flow" in result.error
    assert await store.list() == []


async def test_a_schedule_runs_its_flow_as_north(tool, store) -> None:
    result = await schedule(tool, name="stretch-routine", hour=9, label="Stretch")

    assert result.success, result.error
    (row,) = await store.list()
    assert row["flow"] == "stretch-routine"
    assert row["agent"] == "north" and not row["skill"]


async def test_a_schedule_takes_the_flows_description_as_what_it_does(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store, {"briefing": "active"})

    result = await schedule(tool, name="briefing", hour=7)

    assert result.success, result.error
    (row,) = await store.list()
    assert row["task"] == "Compile the briefing"


async def test_an_unknown_flow_is_refused_when_flows_can_be_checked(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store, {})

    result = await schedule(tool, name="nope", hour=9)

    assert not result.success
    assert "Unknown flow" in result.error
    assert await store.list() == []


async def test_a_one_shot_run_of_a_flow_carries_the_flow(tool, queue) -> None:
    result = await schedule(tool, name="briefing", run_at="2030-01-01T09:00")

    assert result.success, result.error
    assert result.data["type"] == "one-shot"
    assert queue.jobs[-1].payload["flow"] == "briefing"


async def test_a_draft_flow_cannot_be_scheduled_before_activation(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store, {"candidate": "candidate"})

    result = await schedule(tool, name="candidate", hour=9)

    assert not result.success
    assert "not active" in result.error


async def test_the_old_weekday_spelling_still_works(tool) -> None:
    """A caller working from a cached tool description is not simply refused."""
    result = await schedule(tool, hour=9, weekday=2)
    assert result.success and result.data["cadence"] == "every Wed"


async def test_a_nonsense_day_is_reported_in_words(tool) -> None:
    result = await schedule(tool, hour=9, days="whenever")
    assert not result.success
    assert "day of the week" in result.error


async def test_an_hour_out_of_range_names_the_range(tool) -> None:
    result = await schedule(tool, hour=25)
    assert not result.success and "0-23" in result.error


async def test_an_hour_sent_as_a_list_is_reported_not_raised(tool) -> None:
    result = await schedule(tool, hour=[9, 10])
    assert not result.success and "hour" in result.error


async def test_neither_time_nor_date_asks_for_one(tool) -> None:
    result = await schedule(tool)
    assert not result.success and "run_at" in result.error


async def test_two_similar_reminders_both_survive(tool, store) -> None:
    """Names come from truncated task text, so near-identical tasks collided."""
    await call(tool, action="schedule", message="remind me to stretch in the morning please", hour=9)
    await call(tool, action="schedule", message="remind me to stretch in the morning please, and evening", hour=18)
    assert len(await store.list()) == 2


# ---- changing one ----


async def test_days_can_be_changed_after_the_fact(tool, store) -> None:
    await schedule(tool, hour=9, days="weekdays")
    (row,) = await store.list()

    result = await reschedule(tool, schedule=row["name"], days=["sat", "sun"])
    assert result.success, result.error
    assert result.data["cadence"] == "weekends"


async def test_a_day_restriction_can_be_cleared_back_to_daily(tool, store) -> None:
    await schedule(tool, hour=9, days=["tue"])
    (row,) = await store.list()

    result = await reschedule(tool, schedule=row["name"], days="daily")
    assert result.success and result.data["cadence"] == "daily"


async def test_pausing_keeps_the_schedule_but_stops_promising_a_next_run(tool, store) -> None:
    await schedule(tool, hour=9)
    (row,) = await store.list()

    result = await reschedule(tool, schedule=row["name"], enabled=False)
    assert result.success
    assert result.data["enabled"] is False
    assert result.data["next_run"] == "paused"
    assert "(paused)" in tool.format_output(result.data)
    assert len(await store.list()) == 1  # paused, not deleted


async def test_changing_the_time_leaves_the_days_alone(tool, store) -> None:
    await schedule(tool, hour=9, days="weekdays")
    (row,) = await store.list()

    await reschedule(tool, schedule=row["name"], hour=11)
    updated = await store.get(row["name"])
    assert updated["hour"] == 11
    assert updated["weekdays"] == frozenset({0, 1, 2, 3, 4})


async def test_schedule_can_switch_between_wall_clock_and_fixed_interval(tool, store) -> None:
    await schedule(tool, hour=9)
    (row,) = await store.list()

    interval = await reschedule(tool, schedule=row["name"], interval_minutes=10)
    assert interval.success and interval.data["cadence"] == "every 10 minutes"

    wall_clock = await reschedule(tool, schedule=row["name"], hour=11, minute=30)
    assert wall_clock.success
    updated = await store.get(row["name"])
    assert updated["interval_minutes"] is None
    assert (updated["hour"], updated["minute"]) == (11, 30)


async def test_a_builtin_schedule_can_be_retimed_by_asking(tool) -> None:
    """Asking north to move a built-in writes an override seeded from the shipped values."""
    result = await reschedule(tool, schedule="task_context_cleanup", hour=7)

    assert result.success, result.error
    assert result.data["hour"] == 7
    assert result.data["source"] == "builtin"
    # The fields the request did not name keep their shipped values.
    assert result.data["agent"] == "system"


async def test_an_edited_builtin_is_listed_once(tmp_path, queue, store) -> None:
    tool = _tool(tmp_path, queue, store)
    await reschedule(tool, schedule="task_context_cleanup", hour=7)

    grouped = await FlowSchedules(queue, store).by_flow()

    names = [view.get("name") for views in grouped.values() for view in views]
    assert names.count("task_context_cleanup") == 1
    # A row edited before built-ins ran flows stored none; it still runs, and shows under, the shipped flow.
    await store.update("task_context_cleanup", flow="")
    grouped = await FlowSchedules(queue, store).by_flow()
    assert [view["hour"] for view in grouped["nightly-cleanup"] if view["name"] == "task_context_cleanup"] == [7]


async def test_changing_something_that_does_not_exist_says_so(tool) -> None:
    result = await reschedule(tool, schedule="user_nope", hour=10)
    assert not result.success and "List the flows" in result.error


# ---- removing one ----


async def test_unscheduling_an_edited_builtin_restores_the_default(tool, store) -> None:
    await reschedule(tool, schedule="task_context_cleanup", hour=7)

    result = await unschedule(tool, schedule="task_context_cleanup")

    assert result.success and result.data["type"] == "restored"
    assert await store.get("task_context_cleanup") is None  # back to the shipped constant


async def test_an_untouched_builtin_cannot_be_deleted_only_paused(tool) -> None:
    result = await unschedule(tool, schedule="task_context_cleanup")

    assert not result.success
    assert "Pause it" in result.error


async def test_a_one_shot_is_unscheduled_by_its_job_id(tool, queue) -> None:
    created = await schedule(tool, run_at="2030-01-01T09:00")

    result = await unschedule(tool, job_id=created.data["job_id"])

    assert result.success, result.error
    assert queue.jobs[0].status is JobStatus.CANCELLED
    assert "will not run again" in tool.format_output(result.data)


async def test_a_provisioned_default_is_a_deletable_user_schedule(tool, store, sample_provisioned_default) -> None:
    """After provisioning a default is the user's own: unschedule deletes it for good."""
    from jobs.scheduler import provision_default_schedules

    await provision_default_schedules(store)
    assert await store.get("morning_digest") is not None

    result = await unschedule(tool, schedule="morning_digest")

    assert result.success and result.data["type"] != "restored"
    assert await store.get("morning_digest") is None


async def test_provisioning_does_not_resurrect_a_deleted_default(tool, store, sample_provisioned_default) -> None:
    """A provisioned default the user deleted stays gone across restarts."""
    from jobs.scheduler import provision_default_schedules

    await provision_default_schedules(store)
    await unschedule(tool, schedule="morning_digest")

    # A later start runs provisioning again - it must not come back.
    seeded = await provision_default_schedules(store)
    assert seeded == []
    assert await store.get("morning_digest") is None
