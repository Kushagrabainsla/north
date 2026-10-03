"""A real agent uses what north tells it about the user. Skipped unless the CLI is installed and opted in.

NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_briefing_live.py -q
"""

from __future__ import annotations

import os
import shutil

import pytest

from coding_agents import ClaudeBackend, CodexBackend, CodingRunner
from tests.conftest import approving_store, bind_approvals
from tests.unit.coding_agents.conftest import MemoryRecorder
from tools.models import ToolInput
from tools.specialized.coding_agent import CodingAgentTool

FACT = "Facts the user has stated:\n- Every new function the user asks for must be named with the prefix nx_"


class FixedBriefing:
    async def brief(self, task: str, workspace: str) -> str:
        return FACT


def _enabled(flag: str, command: str) -> bool:
    return os.environ.get(flag) == "1" and shutil.which(command) is not None


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            "claude",
            marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CLAUDE", "claude"), reason="NORTH_LIVE_CLAUDE=1"),
        ),
        pytest.param(
            "codex",
            marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CODEX", "codex"), reason="NORTH_LIVE_CODEX=1"),
        ),
    ],
)
async def test_the_agent_follows_a_stated_preference_it_was_briefed_on(repo, tmp_path, backend) -> None:
    backends = {"claude": ClaudeBackend(protected_paths=[str(tmp_path / "north_home")]), "codex": CodexBackend()}
    runner = CodingRunner({backend: backends[backend]}, MemoryRecorder())
    tool = bind_approvals(CodingAgentTool(runner, briefing=FixedBriefing()), store=approving_store())

    result = await tool.execute(
        ToolInput(
            params={
                "task_id": "t-brief",
                "task": "Plan adding a function that subtracts two numbers to calc.py. State the exact function name.",
            },
            granted_workspace=str(repo),
        )
    )

    assert result.success, result.error
    assert "nx_" in result.data["answer"], result.data["answer"]
