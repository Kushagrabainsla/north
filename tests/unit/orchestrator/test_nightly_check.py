"""north runs its nightly check itself; launchd could not open a project folder on the Desktop."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import orchestrator.nightly_check as nightly
from flows.registry import FlowRegistry
from inference.exceptions import missing_resource
from utils.runtime_resources import resource_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(nightly.settings, "north_home", tmp_path / "north")
    monkeypatch.setattr(nightly, "fully_awake", lambda: True)
    return tmp_path / "north"


def _project(tmp_path: Path, home: Path, script: str) -> Path:
    folder = tmp_path / "anywhere" / "north"
    (folder / "scripts").mkdir(parents=True)
    smoke = folder / "scripts" / "smoke.sh"
    smoke.write_text("#!/usr/bin/env bash\n" + script, encoding="utf-8")
    smoke.chmod(0o755)
    (home / "smoke").mkdir(parents=True)
    (home / "smoke" / "checkout").write_text(f"{folder}\n", encoding="utf-8")
    return folder


@pytest.mark.asyncio
async def test_a_pass_says_so_with_the_report(tmp_path: Path, home: Path) -> None:
    _project(tmp_path, home, 'echo "lint ok"\necho "smoke passed: $NORTH_HOME/smoke/r.log live=$SMOKE_LIVE"\n')

    assert await nightly.nightly_check() == f"The nightly check passed: {home}/smoke/r.log live=1"


@pytest.mark.asyncio
async def test_a_failure_names_the_part_that_failed(tmp_path: Path, home: Path) -> None:
    _project(tmp_path, home, 'echo "smoke failed: live coding agents - /r.log"\nexit 1\n')

    with pytest.raises(RuntimeError, match=r"^The nightly check failed: live coding agents - /r.log$"):
        await nightly.nightly_check()


@pytest.mark.asyncio
async def test_it_runs_at_the_lowest_cpu_priority(tmp_path: Path, home: Path) -> None:
    """After a night asleep it can run while you work.

    Nineteen steps below this test's own priority, capped at the bottom (20): run by
    the nightly check itself, the test starts at 19 and its script ends at 20.
    """
    _project(tmp_path, home, 'echo "smoke passed: niceness $(ps -o nice= -p $$ | tr -d " ")"\n')

    assert await nightly.nightly_check() == f"The nightly check passed: niceness {min(os.nice(0) + 19, 20)}"


@pytest.mark.asyncio
async def test_without_a_registered_project_folder_it_says_how_to_register_one(home: Path) -> None:
    with pytest.raises(RuntimeError, match="install_smoke_schedule.sh"):
        await nightly.nightly_check()


@pytest.mark.asyncio
async def test_an_asleep_laptop_waits_for_a_real_wake(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed laptop wakes for seconds at a time; the check takes minutes."""
    _project(tmp_path, home, 'touch "$NORTH_HOME/ran"\necho "smoke passed: x"\n')
    monkeypatch.setattr(nightly, "fully_awake", lambda: False)

    with pytest.raises(Exception) as waiting:
        await nightly.nightly_check()

    assert missing_resource(waiting.value) == "an awake laptop"  # the flow run waits, and resumes later
    assert not (home / "ran").exists()


def test_the_nightly_check_ships_as_a_flow_of_one_system_step() -> None:
    flow = FlowRegistry(resource_path("builtin-flows")).get("nightly-check")

    assert flow.status == "active"
    assert [step.action for step in flow.steps] == ["nightly_check"]
