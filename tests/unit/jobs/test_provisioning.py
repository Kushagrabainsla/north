"""What a fresh install starts with: no schedules of its own, and no one's personal routines."""

from __future__ import annotations

import pytest

from flows.registry import FlowRegistry
from jobs.cron_store import UserCronStore
from jobs.scheduler import PROVISIONED_CRON_ENTRIES, provision_default_schedules
from skills.registry import SkillRegistry
from utils.runtime_resources import builtin_skills_dir, resource_path

PERSONAL_ROUTINES = ("daily-news-briefing", "daily-research-briefing")


@pytest.fixture
def store(tmp_path) -> UserCronStore:
    return UserCronStore(tmp_path / "jobs.db")


async def test_a_fresh_install_starts_with_no_schedules_of_its_own(store) -> None:
    assert PROVISIONED_CRON_ENTRIES == []

    assert await provision_default_schedules(store) == []
    assert await store.list() == []


def test_north_ships_none_of_the_personal_briefing_routines() -> None:
    """A daily news briefing and a research briefing are one person's routines.

    Someone who wants them builds them as flows; a new install must not arrive with
    them, or with a schedule pointing at a flow it does not have.
    """
    flows = FlowRegistry(resource_path("builtin-flows"))
    skills = SkillRegistry(builtin_skills_dir())

    for name in PERSONAL_ROUTINES:
        assert name not in flows.names()
    assert "news-briefing" not in skills.names()
    assert "research-paper-briefing" not in skills.names()


async def test_a_provisioned_schedule_keeps_the_flow_it_runs(store, sample_provisioned_default) -> None:
    """Seeding used to drop the flow, so a default would have run as a bare prompt."""
    seeded = await provision_default_schedules(store)

    assert seeded == ["morning_digest"]
    row = await store.get("morning_digest")
    assert row["flow"] == "morning-digest"
    assert row["label"] == "Morning digest"


async def test_a_provisioned_schedule_is_seeded_once_and_never_resurrected(store, sample_provisioned_default) -> None:
    await provision_default_schedules(store)
    await store.remove("morning_digest")

    assert await provision_default_schedules(store) == []
    assert await store.get("morning_digest") is None
