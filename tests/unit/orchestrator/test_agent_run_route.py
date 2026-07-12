"""Tests for the /agent/run route: it must run the named agent directly.

Guards the fix where `north agent run <name>` was silently re-routed by the
planner instead of invoking the requested agent. The route now validates the
name (404 on unknown) and hands the orchestrator a `forced_agent` task.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import orchestrator.api_router as api
from agents.exceptions import AgentNotFoundError
from orchestrator.models import TaskResponse


class _UnknownRegistry:
    def get(self, name: str):
        raise AgentNotFoundError(f"No agent registered with name: {name}")

    def names(self) -> list[str]:
        return ["coder", "researcher", "reviewer"]


class _KnownRegistry:
    def get(self, name: str):
        return SimpleNamespace(name=name)

    def names(self) -> list[str]:
        return ["coder"]


class _CapturingOrchestrator:
    def __init__(self) -> None:
        self.last_request = None

    async def submit_task(self, request):
        self.last_request = request
        return TaskResponse(task_id="task_1", status="pending", created_at="now")


async def test_run_agent_unknown_name_returns_404(monkeypatch):
    monkeypatch.setattr(api, "_agent_registry", _UnknownRegistry())
    with pytest.raises(HTTPException) as exc:
        await api.run_agent(api.AgentRunRequest(agent="ghost", task="do it"))
    assert exc.value.status_code == 404
    assert "ghost" in str(exc.value.detail)
    assert "coder" in str(exc.value.detail)  # lists the available agents


async def test_run_agent_forwards_forced_agent(monkeypatch):
    orchestrator = _CapturingOrchestrator()
    monkeypatch.setattr(api, "_agent_registry", _KnownRegistry())
    monkeypatch.setattr(api, "_orchestrator", orchestrator)

    response = await api.run_agent(api.AgentRunRequest(agent="coder", task="fix the bug"))

    assert response.task_id == "task_1"
    request = orchestrator.last_request
    assert request.forced_agent == "coder"
    assert request.prompt == "fix the bug"  # raw task, no "[coder]" prefix
