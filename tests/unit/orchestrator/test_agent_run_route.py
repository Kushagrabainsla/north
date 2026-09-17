"""Tests for the /agent/run route: it must run the named agent directly.

Guards the fix where `north agent run <name>` was silently re-routed by the
planner instead of invoking the requested agent. The route now validates the
name (404 on unknown) and hands the orchestrator a `forced_agent` task.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import orchestrator.api.agents as api
from agents.exceptions import AgentNotFoundError
from jobs.models import JobStatus
from orchestrator.api_context import ApiServices, bind_services
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
        return ["coder", "custom"]

    def source_of(self, name: str) -> str:
        return "personal" if name == "custom" else "builtin"

    def remove_personal(self, name: str) -> bool:
        return name == "custom"


class _CapturingOrchestrator:
    def __init__(self) -> None:
        self.last_request = None

    async def submit_task(self, request):
        self.last_request = request
        return TaskResponse(task_id="task_1", status="pending", created_at="now")


class _CronStore:
    def __init__(self) -> None:
        self.removed_agents: list[str] = []

    async def remove_for_agent(self, agent: str) -> list[str]:
        self.removed_agents.append(agent)
        return ["morning", "evening"]


class _JobProcessor:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def list_jobs(self, status=None, limit=100):
        assert status is JobStatus.PENDING
        assert limit == 1_000_000
        return [
            SimpleNamespace(job_id="personal-pending", agent="custom"),
            SimpleNamespace(job_id="other-pending", agent="coder"),
        ]

    async def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


def _delete_services() -> tuple[ApiServices, _CronStore, _JobProcessor]:
    cron_store = _CronStore()
    job_processor = _JobProcessor()
    services = ApiServices(
        agent_registry=_KnownRegistry(),
        cron_store=cron_store,
        job_processor=job_processor,
    )
    return services, cron_store, job_processor


async def test_run_agent_unknown_name_returns_404():
    with bind_services(ApiServices(agent_registry=_UnknownRegistry())), pytest.raises(HTTPException) as exc:
        await api.run_agent(api.AgentRunRequest(agent="ghost", task="do it"))
    assert exc.value.status_code == 404
    assert "ghost" in str(exc.value.detail)
    assert "coder" in str(exc.value.detail)  # lists the available agents


async def test_run_agent_forwards_forced_agent():
    orchestrator = _CapturingOrchestrator()
    services = ApiServices(agent_registry=_KnownRegistry(), orchestrator=orchestrator)

    with bind_services(services):
        response = await api.run_agent(api.AgentRunRequest(agent="coder", task="fix the bug"))

    assert response.task_id == "task_1"
    request = orchestrator.last_request
    assert request.forced_agent == "coder"
    assert request.prompt == "fix the bug"  # raw task, no "[coder]" prefix


async def test_delete_agent_rejects_builtins():
    services, _, _ = _delete_services()
    with bind_services(services), pytest.raises(HTTPException) as exc:
        await api.delete_agent("coder")
    assert exc.value.status_code == 403


async def test_delete_agent_returns_404_for_unknown_name():
    services, _, _ = _delete_services()
    with bind_services(services), pytest.raises(HTTPException) as exc:
        await api.delete_agent("ghost")
    assert exc.value.status_code == 404


async def test_delete_agent_removes_personal_agent_and_its_future_work():
    services, cron_store, job_processor = _delete_services()
    with bind_services(services):
        assert await api.delete_agent("custom") is None

    assert cron_store.removed_agents == ["custom"]
    assert job_processor.cancelled == ["personal-pending"]
