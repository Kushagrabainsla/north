"""North's own work on a clock is a flow, like any other: one mechanism.

Fact upkeep, the morning "Rules to confirm" card, episode notes and skill learning were four
loops, each with its own timer, that no page showed and no card reported when they failed.
Each is now a built-in flow with one fixed step, run by a built-in schedule.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flows import FlowRegistry
from jobs.scheduler import SYSTEM_CRON_ENTRIES, CronEntry, next_firing

_REPO = Path(__file__).parents[3]
_BUILTIN = _REPO / "resources" / "builtin-flows"


def test_every_built_in_schedule_runs_a_built_in_flow_whose_step_north_registers() -> None:
    registry = FlowRegistry(builtin_dir=_BUILTIN, learned_dir=_BUILTIN / "none")
    registered = set(re.findall(r'register_action\("([a-z_]+)"', (_REPO / "orchestrator" / "app.py").read_text()))
    for entry in SYSTEM_CRON_ENTRIES:
        flow = registry.get(entry.flow)
        assert [step.action for step in flow.steps if step.action], entry.name
        assert {step.action for step in flow.steps} <= registered, entry.name


def test_north_s_clock_work_is_on_the_built_in_schedules() -> None:
    by_flow = {entry.flow: entry for entry in SYSTEM_CRON_ENTRIES}
    assert {"fact-upkeep", "rules-to-confirm", "episode-memory", "skill-learning"} <= set(by_flow)
    assert (by_flow["rules-to-confirm"].hour, by_flow["rules-to-confirm"].minute) == (8, 0)
    assert by_flow["episode-memory"].interval_minutes == by_flow["skill-learning"].interval_minutes == 60


def test_an_hourly_flow_keeps_its_minute_after_a_restart() -> None:
    """Anchored at the start, a restart moved them; a fixed anchor keeps them off the full hour."""
    hourly = next(entry for entry in SYSTEM_CRON_ENTRIES if entry.flow == "episode-memory")
    rebuilt = CronEntry(**hourly.__dict__)
    now = datetime(2026, 11, 18, 21, 0, tzinfo=UTC)
    assert next_firing(hourly, now) == next_firing(rebuilt, now)
    assert next_firing(hourly, now).minute == 22
    assert next_firing(hourly, now) - now < timedelta(hours=1)
