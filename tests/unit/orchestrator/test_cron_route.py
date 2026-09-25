"""The schedule API the web page drives: create, change, pause, delete.

The page has to be able to say everything the chat tool can, or a routine
created by asking north becomes one the user cannot edit.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import orchestrator.api.cron as api
from flows.registry import FlowRegistry
from jobs.cron_store import UserCronStore
from jobs.scheduler import builtin_default
from orchestrator.api_context import ApiServices, bind_services


@pytest.fixture
def store(tmp_path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


@pytest.fixture
def flows(tmp_path) -> FlowRegistry:
    """One active flow and one candidate: a schedule needs the first, and refuses the second."""
    for name, status in (("briefing", "active"), ("draft", "candidate")):
        directory = tmp_path / "flows" / name
        directory.mkdir(parents=True)
        (directory / "FLOW.yaml").write_text(
            f"name: {name}\ndescription: Compile the {name}\nstatus: {status}\n"
            "steps:\n  - name: go\n    instructions: Do it.\n    approval: never\n",
            encoding="utf-8",
        )
    return FlowRegistry(tmp_path / "flows")


async def create(**body):
    fields = {"flow": "briefing", "hour": 9, "minute": 30}
    return await api.create_cron_entry(api.CronEntryCreate(**{**fields, **body}))


async def update(name: str, **body):
    return await api.update_cron_entry(name, api.CronEntryUpdate(**body))


@pytest.mark.asyncio
async def test_a_weekday_routine_is_created_from_the_page(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(days="weekdays")

    assert entry.cadence == "weekdays"
    assert entry.weekdays == [0, 1, 2, 3, 4]
    assert entry.enabled is True


@pytest.mark.asyncio
async def test_days_may_be_sent_as_the_numbers_the_day_buttons_produce(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(days=[5, 6])
    assert entry.cadence == "weekends"


@pytest.mark.asyncio
async def test_no_days_means_every_day(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()
    assert entry.cadence == "daily" and entry.weekdays == []


@pytest.mark.asyncio
async def test_fixed_interval_routine_can_be_created_and_edited_from_the_page(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await api.create_cron_entry(api.CronEntryCreate(flow="briefing", interval_minutes=5))
        edited = await update(entry.name, interval_minutes=15)

    assert entry.cadence == "every 5 minutes"
    assert entry.interval_minutes == 5
    assert entry.anchor_epoch is not None
    assert edited.cadence == "every 15 minutes"


@pytest.mark.asyncio
async def test_schedule_api_rejects_ambiguous_timing_modes(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await api.create_cron_entry(api.CronEntryCreate(flow="briefing", hour=9, interval_minutes=5))
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_bad_day_is_a_422_not_a_500(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await create(days="someday")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_impossible_hour_is_refused(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await create(hour=25)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_timezone_is_refused(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await create(tz="Mars/Olympus_Mons")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_two_schedules_of_one_flow_both_survive(store, flows) -> None:
    """The page does not send names, so the server has to keep them apart."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        first = await create()
        second = await create(hour=18)
        listed = await api.list_cron_entries(builtin=False)

    assert first.name != second.name
    assert len(listed) == 2


@pytest.mark.asyncio
async def test_editing_the_time_leaves_the_days_alone(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(days="weekdays")
        edited = await update(entry.name, hour=11)

    assert edited.hour == 11 and edited.cadence == "weekdays"


@pytest.mark.asyncio
async def test_sending_daily_clears_a_day_restriction(store, flows) -> None:
    """An omitted field and a cleared one both arrive as null, so days say "daily"."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(days=["tue"])
        edited = await update(entry.name, days="daily")

    assert edited.cadence == "daily"


@pytest.mark.asyncio
async def test_pausing_and_resuming_from_the_page(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()
        paused = await update(entry.name, enabled=False)
        assert paused.enabled is False
        assert len(await api.list_cron_entries(builtin=False)) == 1  # kept, not deleted

        resumed = await update(entry.name, enabled=True)
    assert resumed.enabled is True


@pytest.mark.asyncio
async def test_deleting_removes_it(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()
        await api.delete_cron_entry(entry.name)
        assert await api.list_cron_entries(builtin=False) == []


@pytest.mark.asyncio
async def test_a_builtin_schedule_can_be_retimed(store, flows) -> None:
    """A system built-in ships in the source; retiming it writes an override row."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        edited = await update("task_context_cleanup", hour=7, minute=15)

    assert (edited.hour, edited.minute) == (7, 15)
    assert edited.source == "builtin"
    assert edited.modified is True


@pytest.mark.asyncio
async def test_editing_a_builtin_keeps_the_fields_it_did_not_name(store, flows) -> None:
    """The override is seeded from the shipped values, not from blanks."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        edited = await update("task_context_cleanup", hour=7)

    assert edited.agent == "system"
    assert edited.flow == "nightly-cleanup"


@pytest.mark.asyncio
async def test_a_builtin_stored_before_it_ran_a_flow_is_pointed_at_its_flow(store, flows) -> None:
    """An edit made under the old shape must not keep running the old way."""
    await store.add(
        name="task_context_cleanup",
        agent="system",
        task="task_context_cleanup",
        hour=4,
        minute=0,
        weekdays=None,
    )

    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        listed = await api.list_cron_entries()

    cleanup = next(entry for entry in listed if entry.name == "task_context_cleanup")
    assert cleanup.hour == 4
    assert cleanup.flow == "nightly-cleanup"


@pytest.mark.asyncio
async def test_a_builtin_can_be_paused(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        paused = await update("task_context_cleanup", enabled=False)
    assert paused.enabled is False


@pytest.mark.asyncio
async def test_an_edited_builtin_is_listed_once_not_twice(store, flows) -> None:
    """Listing the shipped and the stored version would show two schedules where one runs."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        await update("task_context_cleanup", hour=7)
        listed = await api.list_cron_entries()

    cleanups = [e for e in listed if e.name == "task_context_cleanup"]
    assert len(cleanups) == 1
    assert cleanups[0].hour == 7


@pytest.mark.asyncio
async def test_deleting_an_edited_builtin_restores_the_shipped_default(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        await update("task_context_cleanup", hour=7, enabled=False)
        await api.delete_cron_entry("task_context_cleanup")
        listed = await api.list_cron_entries()

    (cleanup,) = [e for e in listed if e.name == "task_context_cleanup"]
    assert cleanup.hour == 3
    assert cleanup.enabled is True
    assert cleanup.modified is False


@pytest.mark.asyncio
async def test_deleting_an_unedited_builtin_says_to_pause_it_instead(store, flows) -> None:
    """It lives in the source, so there is nothing to remove - only to stop."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await api.delete_cron_entry("task_context_cleanup")

    assert exc.value.status_code == 409
    assert "Pause" in exc.value.detail


@pytest.mark.asyncio
async def test_editing_something_that_is_not_there_is_a_404(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(HTTPException) as exc:
        await update("user_nope", hour=10)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_listing_includes_builtins_and_sorts_by_next_run(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        await create(hour=23)
        listed = await api.list_cron_entries()

    assert {e.source for e in listed} == {"user", "builtin"}
    assert [e.next_run_epoch for e in listed] == sorted(e.next_run_epoch for e in listed)


@pytest.mark.asyncio
async def test_a_schedule_can_be_titled_separately_from_its_flow(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(label="Morning briefing")

    assert entry.title == "Morning briefing"
    assert entry.task == "Compile the briefing"
    # The key comes from the title when there is one, so it reads as the schedule.
    assert entry.name == "user_morning_briefing"


@pytest.mark.asyncio
async def test_an_untitled_schedule_is_named_for_its_flow(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()
    assert entry.name == "user_briefing" and entry.label == ""
    assert entry.title == "Compile the briefing"


@pytest.mark.asyncio
async def test_renaming_leaves_the_flow_alone(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(label="Briefing")
        renamed = await update(entry.name, label="Morning news")

    assert renamed.title == "Morning news"
    assert renamed.flow == "briefing"


@pytest.mark.asyncio
async def test_the_builtin_has_a_readable_name(store, flows) -> None:
    """The list showed a slug for the built-in; it must show its title instead."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        listed = await api.list_cron_entries()

    titles = {e.name: e.title for e in listed}
    assert titles["task_context_cleanup"] == "Nightly cleanup"


@pytest.mark.asyncio
async def test_a_provisioned_briefing_lists_as_the_users_own(store, flows) -> None:
    """Once provisioned, the daily briefing is a user schedule, not a built-in.

    It can be retimed, paused, and deleted for good like anything the user made.
    """
    from jobs.scheduler import provision_default_schedules

    await provision_default_schedules(store)
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        listed = await api.list_cron_entries()
        briefing = next(e for e in listed if e.name == "news_daily_briefing")
        assert briefing.source == "user"
        assert briefing.title == "Daily news briefing"
        assert briefing.hour == 8
        # Deletable for good - not the 409 a built-in gives.
        await api.delete_cron_entry("news_daily_briefing")
        after = await api.list_cron_entries()
    assert not any(e.name == "news_daily_briefing" for e in after)


@pytest.mark.asyncio
async def test_a_builtin_says_what_it_is_for(store, flows) -> None:
    """A built-in's prompt can be an internal slug, which explains nothing.

    "task_context_cleanup" was the whole of what the page could say about the
    job that runs at 3 a.m., so the row read as a chore the user had scheduled
    themselves and could no longer remember.
    """
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        listed = await api.list_cron_entries()

    cleanup = next(entry for entry in listed if entry.name == "task_context_cleanup")
    assert cleanup.description
    assert cleanup.description != cleanup.task
    assert all(entry.description for entry in listed if entry.source == "builtin")


@pytest.mark.asyncio
async def test_a_routine_of_the_users_own_carries_no_description(store, flows) -> None:
    """It is explained by the prompt they wrote for it."""
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create(label="Morning stretch")
    assert entry.description == ""


@pytest.mark.asyncio
async def test_retiming_a_builtin_leaves_its_description_alone(store, flows) -> None:
    """What a schedule is for does not change when the hour it runs at does.

    The override is written by a form with no description field, so the shipped
    text has to be put back on the way out or an edited built-in goes silent.
    """
    shipped = builtin_default("task_context_cleanup")
    assert shipped is not None
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        edited = await update("task_context_cleanup", hour=4)
        listed = await api.list_cron_entries()

    assert edited.hour == 4
    assert edited.description == shipped.description
    cleanup = next(entry for entry in listed if entry.name == "task_context_cleanup")
    assert cleanup.description == shipped.description


@pytest.mark.asyncio
async def test_a_schedule_that_runs_a_flow_says_which(store, flows) -> None:
    await store.add(
        name="morning",
        agent="general",
        task="Compile and save the daily news briefing.",
        hour=7,
        minute=0,
        weekdays=None,
        flow="daily-news-briefing",
    )

    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        listed = await api.list_cron_entries(builtin=False)

    entry = next(item for item in listed if item.name == "morning")
    assert entry.flow == "daily-news-briefing"
    assert entry.skill == ""


@pytest.mark.asyncio
async def test_a_schedule_made_from_the_api_carries_its_flow(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()

    assert entry.flow == "briefing"
    assert entry.agent == "general"
    assert (await store.get(entry.name))["flow"] == "briefing"


@pytest.mark.asyncio
async def test_a_schedule_cannot_be_made_without_a_flow(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)), pytest.raises(ValueError):
        api.CronEntryCreate(hour=9)
    assert await store.list() == []


@pytest.mark.asyncio
async def test_a_schedule_is_refused_for_an_unknown_or_unproven_flow(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        with pytest.raises(HTTPException) as unknown:
            await create(flow="nope")
        with pytest.raises(HTTPException) as candidate:
            await create(flow="draft")

    assert unknown.value.status_code == 422 and "Unknown flow" in unknown.value.detail
    assert candidate.value.status_code == 422 and "not active" in candidate.value.detail
    assert await store.list() == []


@pytest.mark.asyncio
async def test_a_schedule_can_be_pointed_at_another_active_flow_but_not_a_candidate(store, flows) -> None:
    with bind_services(ApiServices(cron_store=store, flow_registry=flows)):
        entry = await create()
        with pytest.raises(HTTPException) as refused:
            await update(entry.name, flow="draft")
        unchanged = (await store.get(entry.name))["flow"]

    assert refused.value.status_code == 422
    assert unchanged == "briefing"


def test_a_schedule_update_has_no_prompt_or_agent_to_change() -> None:
    assert "task" not in api.CronEntryUpdate.model_fields
    assert "agent" not in api.CronEntryUpdate.model_fields
