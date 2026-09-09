"""Multi-day recurrence: the rule, how it is stored, and how it is said in words.

A schedule used to be one weekday or every day, which cannot express the most
ordinary request there is - "every weekday at 9:30". Asked for it, the model sent
a list, the tool called `int()` on it, and the task died with "int() argument
must be ... not 'list'".

Every entry here pins tz="UTC" so the firings land on the instants the assertions
are written in, whatever zone the machine running the suite is in.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobs.cron_store import UNSET, UserCronStore, decode_weekdays, encode_weekdays
from jobs.scheduler import WEEKDAYS, WEEKENDS, CronEntry, next_firing, normalise_weekdays, previous_firing

# 2026-05-18 is a Monday, so every date below reads as its weekday name.
MONDAY = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
FRIDAY = datetime(2026, 5, 22, 6, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 5, 23, 6, 0, tzinfo=UTC)


def entry(**kwargs) -> CronEntry:
    fields = {"name": "x", "agent": "general", "task": "t", "hour": 9, "minute": 30, "tz": "UTC"}
    return CronEntry(**{**fields, **kwargs})


# ---- the shape of a selection ----


def test_a_lone_int_is_read_as_that_one_day() -> None:
    """How single-day schedules were stored before the field held a set."""
    assert normalise_weekdays(3) == frozenset({3})


def test_all_seven_days_is_the_same_rule_as_daily() -> None:
    assert normalise_weekdays(frozenset(range(7))) is None


def test_an_empty_selection_is_daily_not_never() -> None:
    """A schedule that runs on no days at all is not a thing anyone wants."""
    assert normalise_weekdays(frozenset()) is None


@pytest.mark.parametrize("bad", [7, -1, 99])
def test_a_day_outside_the_week_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="weekday"):
        normalise_weekdays({bad})


def test_cron_entry_normalises_on_construction() -> None:
    """So every reader downstream can take "None means daily" at face value."""
    assert entry(weekdays=frozenset(range(7))).weekdays is None
    assert entry(weekdays=2).weekdays == frozenset({2})


# ---- when it fires ----


def test_weekday_schedule_skips_the_weekend() -> None:
    """Friday evening's next weekday firing is Monday, not Saturday."""
    assert next_firing(entry(weekdays=WEEKDAYS), FRIDAY.replace(hour=18)).day == 25


def test_weekday_schedule_fires_later_the_same_day_when_the_time_is_still_ahead() -> None:
    fired = next_firing(entry(weekdays=WEEKDAYS), MONDAY)
    assert (fired.day, fired.hour, fired.minute) == (18, 9, 30)


def test_weekend_schedule_from_a_weekday_lands_on_saturday() -> None:
    assert next_firing(entry(weekdays=WEEKENDS), MONDAY).day == 23


def test_two_named_days_pick_whichever_comes_first() -> None:
    """Mon and Thu, asked on Monday evening, is Thursday."""
    assert next_firing(entry(weekdays={0, 3}), MONDAY.replace(hour=20)).day == 21


def test_previous_firing_of_a_weekday_schedule_steps_back_over_the_weekend() -> None:
    """Saturday's most recent weekday firing was Friday."""
    assert previous_firing(entry(weekdays=WEEKDAYS), SATURDAY).day == 22


def test_a_daily_schedule_is_unaffected_by_the_set() -> None:
    assert next_firing(entry(weekdays=None), MONDAY).day == 18


def test_the_wall_clock_survives_a_dst_change() -> None:
    """A 09:30 rule stays 09:30 across a spring-forward, rather than sliding an hour.

    US DST began 2026-03-08. A weekday rule asked on the Friday before it must
    still fire at 09:30 local on the Monday after.
    """
    fired = next_firing(
        entry(weekdays=WEEKDAYS, tz="America/Los_Angeles"),
        datetime(2026, 3, 6, 20, 0, tzinfo=UTC),
    )
    assert (fired.hour, fired.minute) == (9, 30)
    assert fired.day == 9  # the Monday after the change


# ---- how it is said ----


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (None, "daily"),
        (WEEKDAYS, "weekdays"),
        (WEEKENDS, "weekends"),
        (frozenset({1}), "every Tue"),
        (frozenset({0, 3}), "every Mon, Thu"),
        # Six days listed out is a sentence nobody reads to the end of.
        (frozenset({0, 1, 2, 3, 4, 5}), "every day except Sun"),
    ],
)
def test_cadence_reads_as_a_person_would_say_it(days, expected: str) -> None:
    assert entry(weekdays=days).cadence == expected


# ---- how it is stored ----


@pytest.mark.parametrize(
    ("days", "stored"),
    [(None, None), (frozenset({0}), "0"), (WEEKDAYS, "0,1,2,3,4"), (frozenset({6, 5}), "5,6")],
)
def test_weekdays_round_trip_through_storage(days, stored: str | None) -> None:
    assert encode_weekdays(days) == stored
    assert decode_weekdays(stored) == days


def test_an_unreadable_stored_selection_falls_back_to_daily() -> None:
    """A row this cannot parse should still produce a schedule that runs.

    Raising here would take the whole scheduler down over one bad row; a daily
    firing is a visible, fixable wrong.
    """
    assert decode_weekdays("mon,tue") is None


@pytest.mark.asyncio
async def test_a_single_day_row_written_before_sets_still_reads(tmp_path) -> None:
    """Migration: the old `weekday` column means the same as a one-element set."""
    import sqlite3

    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE user_cron_entries (name TEXT PRIMARY KEY, agent TEXT NOT NULL,"
            " task TEXT NOT NULL, hour INTEGER NOT NULL, minute INTEGER NOT NULL,"
            " weekday INTEGER, created_at DATETIME)"
        )
        conn.execute("INSERT INTO user_cron_entries VALUES ('user_old', 'general', 'water', 15, 0, 2, NULL)")

    store = UserCronStore(db)
    (row,) = await store.list()
    assert row["weekdays"] == frozenset({2})
    assert row["enabled"] is True
    assert CronEntry.from_row(row).cadence == "every Wed"


@pytest.mark.asyncio
async def test_storing_and_reading_back_a_weekday_schedule(tmp_path) -> None:
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_stretch", "general", "stretch", 9, 30, WEEKDAYS)
    (row,) = await store.list()
    assert CronEntry.from_row(row).cadence == "weekdays"


@pytest.mark.asyncio
async def test_days_can_be_cleared_back_to_daily(tmp_path) -> None:
    """The update that could not be expressed while omitted and cleared both meant None."""
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_x", "general", "t", 9, 0, WEEKDAYS)
    await store.update("user_x", weekdays=None)
    assert (await store.get("user_x"))["weekdays"] is None


@pytest.mark.asyncio
async def test_an_unmentioned_field_is_left_alone(tmp_path) -> None:
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_x", "general", "t", 9, 0, WEEKDAYS)
    await store.update("user_x", hour=10, weekdays=UNSET)
    row = await store.get("user_x")
    assert row["hour"] == 10
    assert row["weekdays"] == WEEKDAYS


@pytest.mark.asyncio
async def test_two_schedules_whose_text_slugs_alike_do_not_overwrite(tmp_path) -> None:
    """INSERT OR REPLACE plus a truncated name silently deleted the first schedule."""
    store = UserCronStore(tmp_path / "jobs.db")
    morning = "remind me to stretch in the morning please"
    evening = "remind me to stretch in the morning please, and evening"

    first = await store.unique_name(morning)
    await store.add(first, "general", morning, 9, 0, None)
    second = await store.unique_name(evening)
    await store.add(second, "general", evening, 18, 0, None)

    assert first != second
    assert len(await store.list()) == 2


# ---- name, prompt, and key are three different things ----


def test_the_title_is_the_label_when_there_is_one() -> None:
    named = entry(label="Morning stretch", task="remind me to stretch and log it")
    assert named.title == "Morning stretch"


def test_the_title_falls_back_to_the_prompt() -> None:
    """Rows written before labels existed still read as something."""
    assert entry(task="remind me to stretch").title == "remind me to stretch"


@pytest.mark.asyncio
async def test_a_label_round_trips_through_storage(tmp_path) -> None:
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_stretch", "general", "stretch and log it", 9, 30, WEEKDAYS, label="Morning stretch")
    (row,) = await store.list()

    restored = CronEntry.from_row(row)
    assert restored.label == "Morning stretch"
    assert restored.task == "stretch and log it"  # the prompt is untouched by naming
    assert restored.title == "Morning stretch"


@pytest.mark.asyncio
async def test_renaming_does_not_change_what_runs(tmp_path) -> None:
    """The whole point of the split: a title is a label, never an instruction."""
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_x", "general", "remind me to stretch", 9, 0, None, label="Stretch")
    await store.update("user_x", label="Morning mobility")

    row = await store.get("user_x")
    assert row["label"] == "Morning mobility"
    assert row["task"] == "remind me to stretch"


@pytest.mark.asyncio
async def test_an_old_row_without_a_label_still_loads(tmp_path) -> None:
    store = UserCronStore(tmp_path / "jobs.db")
    await store.add("user_old", "general", "water the plants", 9, 0, None)
    (row,) = await store.list()
    assert row["label"] == ""
    assert CronEntry.from_row(row).title == "water the plants"


def test_an_override_without_a_name_inherits_the_shipped_one() -> None:
    """A built-in edited before schedules had names kept showing its slug.

    The stored row holds an empty label, and an empty label must not override a
    good one - the user saw "task_context_cleanup" in their list after the
    update that named it "Nightly cleanup".
    """
    from jobs.scheduler import V1_CRON_ENTRIES, merge_entries

    row = {
        "name": "task_context_cleanup",
        "agent": "system",
        "task": "task_context_cleanup",
        "hour": 3,
        "minute": 0,
        "weekdays": None,
        "tz": "UTC",
        "enabled": True,
        "label": "",
    }
    merged = {e.name: e for e in merge_entries(list(V1_CRON_ENTRIES), [row])}
    assert merged["task_context_cleanup"].title == "Nightly cleanup"


def test_an_override_that_was_renamed_keeps_its_own_name() -> None:
    """Inheriting a missing name must not overwrite one the user chose."""
    from jobs.scheduler import V1_CRON_ENTRIES, merge_entries

    row = {
        "name": "news_daily_briefing",
        "agent": "news_briefing",
        "task": "brief me",
        "hour": 7,
        "minute": 0,
        "weekdays": None,
        "tz": "UTC",
        "enabled": True,
        "label": "My morning news",
    }
    merged = {e.name: e for e in merge_entries(list(V1_CRON_ENTRIES), [row])}
    assert merged["news_daily_briefing"].title == "My morning news"
