"""The four schedule verbs, from the words a model sends to what gets stored.

The case that matters most here is "remind me to stretch every weekday at 9:30".
It reached the tool as a list of days, met `int()`, and came back as
"int() argument must be a string, a bytes-like object or a real number, not
'list'" - an error naming nothing the caller could fix.
"""

from __future__ import annotations

import pytest

from jobs.cron_store import UserCronStore
from tools.models import ToolInput
from tools.universal._schedules import parse_weekdays
from tools.universal.schedule_task import ScheduleTaskTool
from tools.universal.update_schedule import UpdateScheduleTool


class _RecordingProcessor:
    """Stands in for the job queue: one-shot schedules become jobs, not cron rows."""

    def __init__(self) -> None:
        self.jobs: list = []

    async def enqueue(self, job) -> None:
        self.jobs.append(job)


@pytest.fixture
def store(tmp_path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


@pytest.fixture
def tool(store) -> ScheduleTaskTool:
    return ScheduleTaskTool(job_processor=_RecordingProcessor(), cron_store=store)


async def run(tool, **params):
    return await tool.run(ToolInput(params=params))


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


# ---- creating a schedule ----


@pytest.mark.asyncio
async def test_every_weekday_at_930_is_scheduled(tool, store) -> None:
    """The request that used to fail outright."""
    result = await run(tool, task="stretch", hour=9, minute=30, days="weekdays")
    assert result.success, result.error
    assert result.data["cadence"] == "weekdays"
    (row,) = await store.list()
    assert row["weekdays"] == frozenset({0, 1, 2, 3, 4})
    assert (row["hour"], row["minute"]) == (9, 30)


@pytest.mark.asyncio
async def test_a_bare_list_of_days_is_accepted(tool) -> None:
    """What a model actually sends when asked for "every weekday"."""
    result = await run(tool, task="stretch", hour=9, days=[0, 1, 2, 3, 4])
    assert result.success, result.error
    assert result.data["cadence"] == "weekdays"


@pytest.mark.asyncio
async def test_omitting_days_schedules_every_day(tool) -> None:
    result = await run(tool, task="drink water", hour=15)
    assert result.success and result.data["cadence"] == "daily"


@pytest.mark.asyncio
async def test_a_recurring_task_can_reference_a_reusable_skill(tool, store) -> None:
    result = await run(tool, task="find and summarize new roles", skill="job-search", hour=9)
    assert result.success, result.error
    (row,) = await store.list()
    assert row["skill"] == "job-search"


@pytest.mark.asyncio
async def test_a_one_shot_task_carries_its_skill_reference(tool) -> None:
    result = await run(tool, task="prepare the morning digest", skill="daily-digest", run_at="2030-01-01T09:00")
    assert result.success, result.error
    assert result.data["type"] == "one-shot"
    assert tool._job_processor.jobs[-1].payload["skill"] == "daily-digest"


@pytest.mark.asyncio
async def test_unknown_skill_is_rejected_when_a_registry_is_configured(tool, tmp_path) -> None:
    from skills.registry import SkillRegistry

    checked_tool = ScheduleTaskTool(
        job_processor=tool._job_processor,
        cron_store=tool._cron_store,
        skill_registry=SkillRegistry(tmp_path / "builtin"),
    )
    result = await run(checked_tool, task="do work", skill="does-not-exist", hour=9)
    assert not result.success
    assert "Unknown skill" in result.error


@pytest.mark.asyncio
async def test_the_old_weekday_spelling_still_works(tool) -> None:
    """A caller working from a cached tool description is not simply refused."""
    result = await run(tool, task="review", hour=9, weekday=2)
    assert result.success and result.data["cadence"] == "every Wed"


@pytest.mark.asyncio
async def test_a_nonsense_day_is_reported_in_words(tool) -> None:
    result = await run(tool, task="stretch", hour=9, days="whenever")
    assert not result.success
    assert "day of the week" in result.error


@pytest.mark.asyncio
async def test_an_hour_out_of_range_names_the_range(tool) -> None:
    result = await run(tool, task="stretch", hour=25)
    assert not result.success and "0-23" in result.error


@pytest.mark.asyncio
async def test_an_hour_sent_as_a_list_is_reported_not_raised(tool) -> None:
    result = await run(tool, task="stretch", hour=[9, 10])
    assert not result.success and "hour" in result.error


@pytest.mark.asyncio
async def test_neither_time_nor_date_asks_for_one(tool) -> None:
    result = await run(tool, task="stretch")
    assert not result.success and "run_at" in result.error


@pytest.mark.asyncio
async def test_two_similar_reminders_both_survive(tool, store) -> None:
    """Names come from truncated task text, so near-identical tasks collided."""
    await run(tool, task="remind me to stretch in the morning please", hour=9)
    await run(tool, task="remind me to stretch in the morning please, and evening", hour=18)
    assert len(await store.list()) == 2


# ---- changing one ----


@pytest.mark.asyncio
async def test_days_can_be_changed_after_the_fact(tool, store) -> None:
    await run(tool, task="stretch", hour=9, days="weekdays")
    (row,) = await store.list()
    updater = UpdateScheduleTool(cron_store=store)

    result = await updater.run(ToolInput(params={"name": row["name"], "days": ["sat", "sun"]}))
    assert result.success, result.error
    assert result.data["cadence"] == "weekends"


@pytest.mark.asyncio
async def test_a_day_restriction_can_be_cleared_back_to_daily(tool, store) -> None:
    await run(tool, task="stretch", hour=9, days=["tue"])
    (row,) = await store.list()
    updater = UpdateScheduleTool(cron_store=store)

    result = await updater.run(ToolInput(params={"name": row["name"], "days": "daily"}))
    assert result.success and result.data["cadence"] == "daily"


@pytest.mark.asyncio
async def test_pausing_keeps_the_schedule_but_stops_promising_a_next_run(tool, store) -> None:
    await run(tool, task="stretch", hour=9)
    (row,) = await store.list()
    updater = UpdateScheduleTool(cron_store=store)

    result = await updater.run(ToolInput(params={"name": row["name"], "enabled": False}))
    assert result.success
    assert result.data["enabled"] is False
    assert result.data["next_run"] == "paused"
    assert len(await store.list()) == 1  # paused, not deleted


@pytest.mark.asyncio
async def test_changing_the_time_leaves_the_days_alone(tool, store) -> None:
    await run(tool, task="stretch", hour=9, days="weekdays")
    (row,) = await store.list()
    updater = UpdateScheduleTool(cron_store=store)

    await updater.run(ToolInput(params={"name": row["name"], "hour": 11}))
    updated = await store.get(row["name"])
    assert updated["hour"] == 11
    assert updated["weekdays"] == frozenset({0, 1, 2, 3, 4})


@pytest.mark.asyncio
async def test_a_builtin_schedule_can_be_retimed_by_asking(store) -> None:
    """Asking north to move a built-in writes an override seeded from the shipped values."""
    updater = UpdateScheduleTool(cron_store=store)
    result = await updater.run(ToolInput(params={"name": "task_context_cleanup", "hour": 7}))

    assert result.success, result.error
    assert result.data["hour"] == 7
    assert result.data["source"] == "builtin"
    # The fields the request did not name keep their shipped values.
    assert result.data["agent"] == "system"


@pytest.mark.asyncio
async def test_cancelling_an_edited_builtin_restores_the_default(store) -> None:
    from tools.universal.cancel_schedule import CancelScheduleTool

    updater = UpdateScheduleTool(cron_store=store)
    await updater.run(ToolInput(params={"name": "task_context_cleanup", "hour": 7}))

    canceller = CancelScheduleTool(job_processor=None, cron_store=store)
    result = await canceller.run(ToolInput(params={"name": "task_context_cleanup"}))

    assert result.success and result.data["type"] == "restored"
    assert await store.get("task_context_cleanup") is None  # back to the shipped constant


@pytest.mark.asyncio
async def test_an_untouched_builtin_cannot_be_deleted_only_paused(store) -> None:
    from tools.universal.cancel_schedule import CancelScheduleTool

    canceller = CancelScheduleTool(job_processor=None, cron_store=store)
    result = await canceller.run(ToolInput(params={"name": "task_context_cleanup"}))

    assert not result.success
    assert "Pause it" in result.error


@pytest.mark.asyncio
async def test_a_provisioned_briefing_is_a_deletable_user_schedule(store) -> None:
    """After provisioning the briefing is the user's own: cancel deletes it for good."""
    from jobs.scheduler import provision_default_schedules
    from tools.universal.cancel_schedule import CancelScheduleTool

    await provision_default_schedules(store)
    assert await store.get("news_daily_briefing") is not None

    canceller = CancelScheduleTool(job_processor=None, cron_store=store)
    result = await canceller.run(ToolInput(params={"name": "news_daily_briefing"}))

    assert result.success and result.data["type"] != "restored"
    assert await store.get("news_daily_briefing") is None


@pytest.mark.asyncio
async def test_provisioning_does_not_resurrect_a_deleted_briefing(store) -> None:
    """A provisioned default the user deleted stays gone across restarts."""
    from jobs.scheduler import provision_default_schedules
    from tools.universal.cancel_schedule import CancelScheduleTool

    await provision_default_schedules(store)
    canceller = CancelScheduleTool(job_processor=None, cron_store=store)
    await canceller.run(ToolInput(params={"name": "news_daily_briefing"}))

    # A later start runs provisioning again - it must not come back.
    seeded = await provision_default_schedules(store)
    assert seeded == []
    assert await store.get("news_daily_briefing") is None


@pytest.mark.asyncio
async def test_updating_something_that_does_not_exist_says_so(store) -> None:
    updater = UpdateScheduleTool(cron_store=store)
    result = await updater.run(ToolInput(params={"name": "user_nope", "hour": 10}))
    assert not result.success and "list_schedules" in result.error
