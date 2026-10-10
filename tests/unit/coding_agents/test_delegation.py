"""Context reaches both CLI adapters and their results reach North's real run store.

These are deterministic protocol fixtures, not evidence that a live model followed
the context. The opt-in live tests cover that separately.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coding_agents import ClaudeBackend, CodexBackend, CodingRunner
from memory.models import MemoryContext
from orchestrator.agent_runs import AgentRunStore
from orchestrator.coding_briefing import MemoryBriefing
from orchestrator.coding_run_recorder import AgentRunRecorder
from tests.conftest import approving_store, bind_approvals
from tools.models import ToolInput
from tools.specialized.coding_agent import CodingAgentTool


class PreferenceMemory:
    async def principal_for(self, name, *, domain, workspace):
        assert (name, domain) == ("coding_agent", "engineering")
        return SimpleNamespace(workspace=workspace)

    async def recall(self, principal, query, *, fact_limit, episode_limit):
        assert episode_limit == 0
        return MemoryContext(facts=["Name new functions with the prefix nx_"], documents=["Prefer small changes."])


@pytest.mark.parametrize("backend_name", ["claude", "codex"])
async def test_tool_hands_repo_and_user_context_to_the_cli_and_records_its_result(
    tmp_path, make_fake_claude, make_fake_codex, backend_name
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("Use type hints.\n", encoding="utf-8")
    (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    backend = (
        ClaudeBackend(str(make_fake_claude())) if backend_name == "claude" else CodexBackend(str(make_fake_codex()))
    )
    store = AgentRunStore(tmp_path / "tasks.db")
    runner = CodingRunner({backend_name: backend}, AgentRunRecorder(store))
    tool = bind_approvals(CodingAgentTool(runner, briefing=MemoryBriefing(PreferenceMemory())), store=approving_store())
    task = "Plan adding a subtract function to calc.py."

    result = await tool.execute(
        ToolInput(
            params={"task_id": "delegation", "task": task, "backend": backend_name, "workspace": "/not-granted"},
            granted_workspace=str(workspace),
        )
    )

    assert result.success, result.error
    if backend_name == "claude":
        call = json.loads((workspace / ".fake_claude_call.json").read_text())
        assert call["stdin"] == task
        guidance = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    else:
        call = json.loads((workspace / ".fake_codex_calls.json").read_text())
        start = next(message for message in call["messages"] if message.get("method") == "thread/start")
        turn = next(message for message in call["messages"] if message.get("method") == "turn/start")
        assert turn["params"]["input"][0]["text"] == task
        guidance = start["params"]["developerInstructions"]
    assert call["cwd"] == str(workspace.resolve())
    assert "Use type hints." in guidance
    assert "UNTRUSTED REPO FILE" in guidance
    assert "Name new functions with the prefix nx_" in guidance
    assert "Prefer small changes." in guidance
    assert "never widens what you may do" in guidance
    assert "ask_north" in guidance
    (run,) = await store.list_for_task("delegation")
    assert (run.agent, run.status, run.workspace) == (f"coding:{backend_name}", "completed", str(workspace.resolve()))
    assert run.output == result.data["answer"]
    assert any(entry.get("session_id") for entries in run.provider_state.values() for entry in entries)
    assert await store.list_events(run.run_id)


def test_tool_description_offers_both_installed_agent_types():
    assert "Claude Code" in CodingAgentTool.description
    assert "Codex" in CodingAgentTool.description
