"""The schedule API the web page drives: create, change, pause, delete.

The page has to be able to say everything the chat tool can, or a routine
created by asking north becomes one the user cannot edit.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import orchestrator.api.cron as api
from jobs.cron_store import UserCronStore
from orchestrator.api_context import ApiServices, bind_services


@pytest.fixture
def store(tmp_path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


async def create(**body):
    fields = {"task": "stretch", "hour": 9, "minute": 30}
    return await api.create_cron_entry(api.CronEntryCreate(**{**fields, **body}))


async def update(name: str, **body):
    return await api.update_cron_entry(name, api.CronEntryUpdate(**body))


@pytest.mark.asyncio
async def test_a_weekday_routine_is_created_from_the_page(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(days="weekdays")

    assert entry.cadence == "weekdays"
    assert entry.weekdays == [0, 1, 2, 3, 4]
    assert entry.enabled is True


@pytest.mark.asyncio
async def test_days_may_be_sent_as_the_numbers_the_day_buttons_produce(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(days=[5, 6])
    assert entry.cadence == "weekends"


@pytest.mark.asyncio
async def test_no_days_means_every_day(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create()
    assert entry.cadence == "daily" and entry.weekdays == []


@pytest.mark.asyncio
async def test_a_bad_day_is_a_422_not_a_500(store) -> None:
    with bind_services(ApiServices(cron_store=store)), pytest.raises(HTTPException) as exc:
        await create(days="someday")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_impossible_hour_is_refused(store) -> None:
    with bind_services(ApiServices(cron_store=store)), pytest.raises(HTTPException) as exc:
        await create(hour=25)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_two_routines_with_similar_text_both_survive(store) -> None:
    """The page does not send names, so the server has to keep them apart."""
    with bind_services(ApiServices(cron_store=store)):
        first = await create(task="remind me to stretch in the morning please")
        second = await create(task="remind me to stretch in the morning please, and evening")
        listed = await api.list_cron_entries(builtin=False)

    assert first.name != second.name
    assert len(listed) == 2


@pytest.mark.asyncio
async def test_editing_the_time_leaves_the_days_alone(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(days="weekdays")
        edited = await update(entry.name, hour=11)

    assert edited.hour == 11 and edited.cadence == "weekdays"


@pytest.mark.asyncio
async def test_sending_daily_clears_a_day_restriction(store) -> None:
    """An omitted field and a cleared one both arrive as null, so days say "daily"."""
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(days=["tue"])
        edited = await update(entry.name, days="daily")

    assert edited.cadence == "daily"


@pytest.mark.asyncio
async def test_pausing_and_resuming_from_the_page(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create()
        paused = await update(entry.name, enabled=False)
        assert paused.enabled is False
        assert len(await api.list_cron_entries(builtin=False)) == 1  # kept, not deleted

        resumed = await update(entry.name, enabled=True)
    assert resumed.enabled is True


@pytest.mark.asyncio
async def test_deleting_removes_it(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create()
        await api.delete_cron_entry(entry.name)
        assert await api.list_cron_entries(builtin=False) == []


@pytest.mark.asyncio
async def test_a_builtin_schedule_can_be_retimed(store) -> None:
    """The briefing was created by asking north, then written into the source.

    From then on the person whose briefing it was could not move it.
    """
    with bind_services(ApiServices(cron_store=store)):
        edited = await update("news_daily_briefing", hour=7, minute=15)

    assert (edited.hour, edited.minute) == (7, 15)
    assert edited.source == "builtin"
    assert edited.modified is True


@pytest.mark.asyncio
async def test_editing_a_builtin_keeps_the_fields_it_did_not_name(store) -> None:
    """The override is seeded from the shipped values, not from blanks."""
    with bind_services(ApiServices(cron_store=store)):
        edited = await update("news_daily_briefing", hour=7)

    assert edited.agent == "news_briefing"
    assert "news briefing" in edited.task


@pytest.mark.asyncio
async def test_a_builtin_can_be_paused(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        paused = await update("news_daily_briefing", enabled=False)
    assert paused.enabled is False


@pytest.mark.asyncio
async def test_an_edited_builtin_is_listed_once_not_twice(store) -> None:
    """Listing the shipped and the stored version would show two schedules where one runs."""
    with bind_services(ApiServices(cron_store=store)):
        await update("news_daily_briefing", hour=7)
        listed = await api.list_cron_entries()

    briefings = [e for e in listed if e.name == "news_daily_briefing"]
    assert len(briefings) == 1
    assert briefings[0].hour == 7


@pytest.mark.asyncio
async def test_deleting_an_edited_builtin_restores_the_shipped_default(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        await update("news_daily_briefing", hour=7, enabled=False)
        await api.delete_cron_entry("news_daily_briefing")
        listed = await api.list_cron_entries()

    (briefing,) = [e for e in listed if e.name == "news_daily_briefing"]
    assert briefing.hour == 8
    assert briefing.enabled is True
    assert briefing.modified is False


@pytest.mark.asyncio
async def test_deleting_an_unedited_builtin_says_to_pause_it_instead(store) -> None:
    """It lives in the source, so there is nothing to remove - only to stop."""
    with bind_services(ApiServices(cron_store=store)), pytest.raises(HTTPException) as exc:
        await api.delete_cron_entry("news_daily_briefing")

    assert exc.value.status_code == 409
    assert "Pause" in exc.value.detail


@pytest.mark.asyncio
async def test_editing_something_that_is_not_there_is_a_404(store) -> None:
    with bind_services(ApiServices(cron_store=store)), pytest.raises(HTTPException) as exc:
        await update("user_nope", hour=10)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_listing_includes_builtins_and_sorts_by_next_run(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        await create(hour=23)
        listed = await api.list_cron_entries()

    assert {e.source for e in listed} == {"user", "builtin"}
    assert [e.next_run_epoch for e in listed] == sorted(e.next_run_epoch for e in listed)


@pytest.mark.asyncio
async def test_a_routine_can_be_named_separately_from_its_prompt(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(label="Morning stretch", task="remind me to stretch and log it")

    assert entry.title == "Morning stretch"
    assert entry.task == "remind me to stretch and log it"
    # The key comes from the name when there is one, so it reads as the routine.
    assert entry.name == "user_morning_stretch"


@pytest.mark.asyncio
async def test_an_unnamed_routine_is_still_titled_by_its_prompt(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(task="water the plants")
    assert entry.title == "water the plants" and entry.label == ""


@pytest.mark.asyncio
async def test_renaming_leaves_the_prompt_alone(store) -> None:
    with bind_services(ApiServices(cron_store=store)):
        entry = await create(label="Stretch", task="remind me to stretch")
        renamed = await update(entry.name, label="Morning mobility")

    assert renamed.title == "Morning mobility"
    assert renamed.task == "remind me to stretch"


@pytest.mark.asyncio
async def test_the_builtin_briefing_has_a_readable_name(store) -> None:
    """The list showed a slug for one built-in and a 90-character sentence for the other."""
    with bind_services(ApiServices(cron_store=store)):
        listed = await api.list_cron_entries()

    titles = {e.name: e.title for e in listed}
    assert titles["news_daily_briefing"] == "Daily news briefing"
    assert titles["task_context_cleanup"] == "Nightly cleanup"
