"""north runs its nightly check itself; launchd could not open a project folder on the Desktop."""

from __future__ import annotations

import os
import subprocess
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
    monkeypatch.setattr(nightly, "installed_commit", lambda: None)  # the registered folder, unless a test says
    _agents(monkeypatch)  # no coding agent installed, unless a test says otherwise
    return tmp_path / "north"


def _agents(monkeypatch: pytest.MonkeyPatch, *agents: Readiness) -> None:
    async def try_agents() -> list[Readiness]:
        return list(agents)

    monkeypatch.setattr(nightly, "try_agents", try_agents)


def _project(tmp_path: Path, home: Path, script: str) -> Path:
    folder = tmp_path / "anywhere" / "north"
    (folder / "scripts").mkdir(parents=True)
    smoke = folder / "scripts" / "smoke.sh"
    smoke.write_text("#!/usr/bin/env bash\n" + script, encoding="utf-8")
    smoke.chmod(0o755)
    (home / "smoke").mkdir(parents=True)
    (home / "smoke" / "checkout").write_text(f"{folder}\n", encoding="utf-8")
    return folder


def _inconclusive_run() -> str:
    return """
cat >"${SMOKE_REPORT%.log}.live-coding-agents.xml" <<'JUNIT'
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="1" skipped="1">
<testcase classname="tests.live.test_codex_live" name="test_guard">
<skipped message="inconclusive: the agent ran no check">x</skipped></testcase>
</testsuite></testsuites>
JUNIT
echo "smoke passed: /r.log"
"""


@pytest.mark.asyncio
async def test_a_test_that_could_not_tell_passes_until_it_has_said_nothing_too_long(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nightly, "installed_commit", lambda: None)
    _project(tmp_path, home, _inconclusive_run())

    assert "passed" in await nightly.nightly_check()
    assert "passed" in await nightly.nightly_check()
    with pytest.raises(RuntimeError, match="could not tell for 3 runs in a row.*test_guard"):
        await nightly.nightly_check()


def test_a_test_that_tells_again_starts_its_count_over(tmp_path: Path, home: Path) -> None:
    (home / "smoke").mkdir(parents=True)
    (home / "smoke" / "inconclusive.json").write_text('{"tests.live::t": 2}', encoding="utf-8")

    assert nightly._still_inconclusive(home / "smoke" / "r.log") == []

    assert (home / "smoke" / "inconclusive.json").read_text(encoding="utf-8").strip() == "{}"


def test_the_installed_commit_comes_from_how_north_was_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Installed:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return '{"url": "https://x.test/north", "vcs_info": {"vcs": "git", "commit_id": "abc123"}}'

    monkeypatch.setattr(nightly.metadata, "distribution", lambda name: _Installed())

    assert nightly.installed_commit() == ("https://x.test/north", "abc123")


def test_north_not_installed_from_git_has_no_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> None:
        raise nightly.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(nightly.metadata, "distribution", missing)

    assert nightly.installed_commit() is None


@pytest.mark.asyncio
async def test_it_tests_the_installed_commit_and_says_which(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _project(tmp_path, home, 'echo "smoke passed: /r.log"\n')
    prepared: list[tuple[str, str]] = []

    async def copy_of(url: str, commit: str) -> Path:
        prepared.append((url, commit))
        return folder

    monkeypatch.setattr(nightly, "installed_commit", lambda: ("https://x.test/north", "abcdef1234567"))
    monkeypatch.setattr(nightly, "_copy_of_commit", copy_of)

    result = await nightly.nightly_check()

    assert prepared == [("https://x.test/north", "abcdef1234567")]
    assert "at abcdef1" in result


@pytest.mark.asyncio
async def test_a_copy_is_made_at_exactly_that_commit(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    for arguments in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
        subprocess.run(["git", *arguments], cwd=origin, check=True)
    (origin / "a.txt").write_text("first\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=origin, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True).stdout.strip()
    (origin / "a.txt").write_text("second\n")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=origin, check=True)
    subprocess.run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=origin, check=True)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    monkeypatch.setattr(nightly.shutil, "which", lambda name: str(uv))

    folder = await nightly._copy_of_commit(f"file://{origin}", commit)

    assert (folder / "a.txt").read_text() == "first\n"


@pytest.mark.asyncio
async def test_a_pass_says_so_with_the_report(tmp_path: Path, home: Path) -> None:
    _project(tmp_path, home, 'echo "lint ok"\necho "smoke passed: $NORTH_HOME/smoke/r.log live=$SMOKE_LIVE"\n')

    assert await nightly.nightly_check() == f"The nightly check passed: {home}/smoke/r.log live=1"


@pytest.mark.asyncio
async def test_a_failure_without_results_says_so_and_points_at_the_report(tmp_path: Path, home: Path) -> None:
    _project(tmp_path, home, 'echo "smoke failed: live coding agents - /r.log"\nexit 1\n')

    with pytest.raises(nightly.NightlyCheckFailed) as failure:
        await nightly.nightly_check()

    [problem] = failure.value.problems
    assert problem.issue == "The nightly check failed: live coding agents - /r.log."
    assert "report" in problem.fix


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
async def test_a_failure_lists_each_cause_with_its_fix_most_common_first(tmp_path: Path, home: Path) -> None:
    """The run said only which stage failed; all its failures were one logged-out agent."""
    folder = _project(tmp_path, home, _FAILED_LIVE_RUN)

    with pytest.raises(nightly.NightlyCheckFailed) as failure:
        await nightly.nightly_check()

    first, second = failure.value.problems
    assert first.issue == (
        f"2 tests in live coding agents failed: {_LOGGED_OUT} "
        "(for example tests/live/test_claude_live.py::test_a_plan_run)."
    )
    assert first.fix == (
        "A change in north broke it (no passing check is on record yet). In "
        f"{folder}, run `NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest "
        "tests/live/test_claude_live.py::test_a_plan_run`."
    )
    assert second.issue.startswith("1 test in live coding agents failed: AssertionError: the change did not land.")
    text = str(failure.value)
    assert text.startswith("The nightly check found 2 problems.\n\n1. Problem: ")
    assert "\n   Fix: " in text and text.endswith(".log")
    assert not list((home / "smoke").glob("*.xml")), "the results are read once; the report is what stays"


@pytest.mark.asyncio
async def test_an_agent_that_cannot_take_a_run_is_not_tested_and_its_fix_is_named(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nov 21: 28 of 50 live tests failed, all because Claude Code was logged out; the card showed them all."""
    _project(tmp_path, home, 'echo "live claude=$NORTH_LIVE_CLAUDE codex=$NORTH_LIVE_CODEX"\necho "smoke passed: x"\n')
    _agents(
        monkeypatch,
        Readiness("codex", installed=True, ready=True, version="0.165.0"),
        Readiness("claude", installed=True, detail="Claude Code is not logged in; run `claude auth login`"),
        Readiness("gemini", installed=False, detail="`gemini` is not installed"),
    )

    with pytest.raises(nightly.NightlyCheckFailed) as failure:
        await nightly.nightly_check()

    [problem] = failure.value.problems
    assert problem.issue == "claude cannot take a coding run, so north did not test it tonight."
    assert problem.fix == "Claude Code is not logged in; run `claude auth login`."
    assert not (home / "smoke" / "last_pass.json").exists(), "a check that skipped an agent is not a pass"


@pytest.mark.asyncio
async def test_the_agents_it_cannot_run_are_switched_off_for_the_live_tests(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, home, 'echo "$NORTH_LIVE_CLAUDE$NORTH_LIVE_CODEX" >"$NORTH_HOME/live"\necho "smoke passed"\n')
    out_of_credit = Readiness(
        "codex",
        installed=True,
        detail="codex could not answer a one-line request: Your workspace is out of credits",
        fix="Wait for codex's usage limit to reset, or add credit to its account.",
    )
    _agents(monkeypatch, Readiness("claude", installed=True, ready=True), out_of_credit)

    with pytest.raises(nightly.NightlyCheckFailed) as failure:
        await nightly.nightly_check()

    assert (home / "live").read_text().strip() == "10"
    [problem] = failure.value.problems
    assert problem.issue.endswith(
        "tonight. codex could not answer a one-line request: Your workspace is out of credits."
    )
    assert problem.fix == out_of_credit.fix


def _git_project(tmp_path: Path, home: Path, script: str) -> Path:
    folder = _project(tmp_path, home, script)
    for arguments in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
        subprocess.run(["git", *arguments], cwd=folder, check=True)
    subprocess.run(["git", "add", "-A"], cwd=folder, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=folder, check=True)
    return folder


@pytest.mark.asyncio
async def test_a_failure_names_the_commits_since_the_last_pass(tmp_path: Path, home: Path) -> None:
    """A code failure's fix says where to look: the commits made since the check last passed."""
    folder = _git_project(tmp_path, home, 'echo "smoke passed: x"\n')
    assert "passed" in await nightly.nightly_check()
    passed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True).stdout.strip()
    (folder / "scripts" / "smoke.sh").write_text("#!/usr/bin/env bash\n" + _FAILED_LIVE_RUN, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=folder, check=True)

    with pytest.raises(nightly.NightlyCheckFailed) as failure:
        await nightly.nightly_check()

    assert f"in the one commit since the last pass ({passed[:7]}, " in failure.value.problems[0].fix


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

    assert "Problem: lint failed: F401 unused import os / --> a.py:1:8" in str(failure.value)


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
