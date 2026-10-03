"""The real `claude`, end to end. Skipped unless NORTH_LIVE_CLAUDE=1: it spends a little of the user's plan.

Proves what the fake cannot: that the flags north passes are accepted by the installed Claude Code,
that a plan run leaves the repository untouched, and that a session really resumes.

    NORTH_LIVE_CLAUDE=1 .venv/bin/python -m pytest tests/live -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agents import ClaudeBackend, CodingRunner, LiveRun
from orchestrator.agent_runs import AgentRunStore
from orchestrator.coding_run_recorder import AgentRunRecorder
from tests.conftest import approving_store, bind_approvals
from tests.unit.coding_agents.conftest import MemoryRecorder
from tools.models import ToolInput
from tools.specialized.coding_agent import CodingAgentTool

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="set NORTH_LIVE_CLAUDE=1 with claude installed to run against the real CLI",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    (directory / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (directory / "CLAUDE.md").write_text("Use type hints.\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=directory, check=True)
    subprocess.run([*git, "add", "."], cwd=directory, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=directory, check=True)
    return directory


def _status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout


async def test_a_plan_run_answers_from_the_repo_and_changes_nothing(repo, tmp_path) -> None:
    store = AgentRunStore(tmp_path / "tasks.db")
    runner = CodingRunner(
        {"claude": ClaudeBackend(protected_paths=[str(tmp_path / "north_home")])}, AgentRunRecorder(store)
    )
    tool = bind_approvals(CodingAgentTool(runner), store=approving_store())

    result = await tool.execute(
        ToolInput(
            params={
                "task_id": "t-live",
                "task": "Read calc.py and plan how to add a subtract function. Do not edit anything.",
            },
            granted_workspace=str(repo),
        )
    )

    assert result.success, result.error
    assert "subtract" in result.data["answer"].lower()
    assert _status(repo) == "", "a plan run must leave the repository exactly as it found it"
    (run,) = await store.list_for_task("t-live")
    assert (run.agent, run.status) == ("coding:claude", "completed")
    state = {key: value for entry in run.provider_state["claude_code"] for key, value in entry.items()}
    assert state["session_id"] and state["pid"] and state["cli_version"]
    assert run.cost_usd >= 0


async def test_a_session_resumes_with_what_it_already_read(repo) -> None:
    first = MemoryRecorder()
    runner = CodingRunner({"claude": ClaudeBackend()}, first)
    start = await runner.run(
        task_id="t1", task="Read calc.py. Remember the name of the function in it. Reply OK.", workspace=str(repo)
    )
    assert start.outcome.ok, start.outcome.error
    session = first.state[start.run_id]["session_id"]

    again = MemoryRecorder(live=LiveRun(run_id=start.run_id, session_id=session))
    follow = await CodingRunner({"claude": ClaudeBackend()}, again).run(
        task_id="t1",
        task="What was the name of the function in the file you read? Reply with the name only.",
        workspace=str(repo),
    )

    assert follow.outcome.ok, follow.outcome.error
    assert "add" in follow.outcome.text.lower()
    assert again.state[start.run_id]["resumed"] is True
    assert _status(repo) == ""
