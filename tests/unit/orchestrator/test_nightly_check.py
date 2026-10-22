"""north runs its nightly check itself; launchd could not open a project folder on the Desktop."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import orchestrator.nightly_check as nightly
from coding_agents.discovery import Readiness
from flows.registry import FlowRegistry
from inference.exceptions import missing_resource
from utils.runtime_resources import resource_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(nightly.settings, "north_home", tmp_path / "north")
    monkeypatch.setattr(nightly, "fully_awake", lambda: True)
    _agents(monkeypatch)  # no coding agent installed, unless a test says otherwise
    return tmp_path / "north"


def _agents(monkeypatch: pytest.MonkeyPatch, *agents: Readiness) -> None:
    async def check_agents() -> list[Readiness]:
        return list(agents)

    monkeypatch.setattr(nightly, "check_agents", check_agents)


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


_LOGGED_OUT = "BackendUnavailableError: claude: Claude Code is not logged in; run `claude auth login`"

_FAILED_LIVE_RUN = f"""
cat >"$SMOKE_REPORT" <<'REPORT'
=== lint (03:30:00)
All checks passed!
--- lint: passed
=== live coding agents (03:31:00)
FFF.
--- live coding agents: FAILED
VERDICT: failed: live coding agents
REPORT
cat >"${{SMOKE_REPORT%.log}}.live-coding-agents.xml" <<'JUNIT'
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" failures="3" errors="0" skipped="0">
<testcase classname="tests.live.test_claude_live" name="test_a_plan_run">
<failure message="{_LOGGED_OUT}">traceback</failure></testcase>
<testcase classname="tests.live.test_claude_live" name="test_an_edit_lands">
<failure message="{_LOGGED_OUT}">traceback</failure></testcase>
<testcase classname="tests.live.test_telegram_live" name="test_a_task_lands">
<failure message="AssertionError: the change did not land.&#10;replies: []">traceback</failure></testcase>
<testcase classname="tests.live.test_codex_live" name="test_ok"/>
</testsuite></testsuites>
JUNIT
echo "smoke failed: live coding agents - $SMOKE_REPORT"
exit 1
"""


@pytest.mark.asyncio
async def test_a_failure_names_its_causes_most_common_first(tmp_path: Path, home: Path) -> None:
    """The run said only which stage failed; all its failures were one logged-out agent."""
    _project(tmp_path, home, _FAILED_LIVE_RUN)

    with pytest.raises(RuntimeError) as failure:
        await nightly.nightly_check()

    lines = str(failure.value).splitlines()
    assert lines[0].startswith("The nightly check failed: live coding agents - ")
    assert lines[1:] == [
        "live coding agents: 3 of 4 tests failed",
        f"- 2 × {_LOGGED_OUT} (e.g. tests.live.test_claude_live::test_a_plan_run)",
        "- 1 × AssertionError: the change did not land. (e.g. tests.live.test_telegram_live::test_a_task_lands)",
    ]
    assert not list((home / "smoke").glob("*.xml")), "the results are read once; the report is what stays"


@pytest.mark.asyncio
async def test_a_failure_says_what_each_installed_coding_agent_needs(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every installed agent is checked; one that cannot take a run says what it needs."""
    _project(tmp_path, home, _FAILED_LIVE_RUN)
    _agents(
        monkeypatch,
        Readiness("codex", installed=True, ready=True, version="0.130.0"),
        Readiness("claude", installed=True, detail="Claude Code is not logged in; run `claude auth login`"),
        Readiness("gemini", installed=False, detail="`gemini` is not installed"),
    )

    with pytest.raises(RuntimeError) as failure:
        await nightly.nightly_check()

    lines = str(failure.value).splitlines()
    assert lines[1:4] == [
        "Coding agents:",
        "- codex: ready, 0.130.0",
        "- claude: not ready. Claude Code is not logged in; run `claude auth login`",
    ]
    assert lines[4] == "live coding agents: 3 of 4 tests failed"


@pytest.mark.asyncio
async def test_a_check_cut_off_before_its_verdict_waits_and_runs_again(tmp_path: Path, home: Path) -> None:
    """North stopped mid-check on Oct 20, and the cut-off check was recorded as a failure with no cause."""
    _project(tmp_path, home, 'echo "=== live coding agents" >"$SMOKE_REPORT"\nkill -TERM $$\n')

    with pytest.raises(Exception) as waiting:
        await nightly.nightly_check()

    assert missing_resource(waiting.value) == "an uninterrupted check"
    assert "signal 15" in str(waiting.value)


@pytest.mark.asyncio
async def test_a_stage_without_test_results_shows_the_start_of_its_output(tmp_path: Path, home: Path) -> None:
    _project(
        tmp_path,
        home,
        'printf "=== lint (03:30:00)\\nF401 unused import os\\n  --> a.py:1:8\\n\\n'
        '--- lint: FAILED\\n" >"$SMOKE_REPORT"\n'
        'echo "smoke failed: lint - $SMOKE_REPORT"\nexit 1\n',
    )

    with pytest.raises(RuntimeError) as failure:
        await nightly.nightly_check()

    assert str(failure.value).splitlines()[1:] == ["lint:", "F401 unused import os", "  --> a.py:1:8"]


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
