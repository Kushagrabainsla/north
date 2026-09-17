"""Agent registry: list, run one directly, scaffold a new one."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from agents.exceptions import AgentNotFoundError
from inference.models import CompletionRequest, PoolPriority
from jobs.models import JobStatus
from orchestrator.api.deps import (
    _get_agent_registry,
    _get_cron_store,
    _get_inference_router,
    _get_job_processor,
    _get_orchestrator,
    router,
)
from orchestrator.models import TaskRequest, TaskResponse
from utils.prompts import load_prompt


class AgentInfo(BaseModel):
    name: str
    domain: str
    model_pool: str = "reasoning"
    accepts: list[str] = []
    source: str = "builtin"
    deletable: bool = False


class AgentRunRequest(BaseModel):
    agent: str
    task: str
    context: str | None = None

    @field_validator("task")
    @classmethod
    def _non_empty_task(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("task must be a non-empty string")
        return v


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents() -> list[AgentInfo]:
    """List all registered domain-specialist agents."""
    registry = _get_agent_registry()
    return [
        AgentInfo(
            name=a.name,
            domain=a.domain,
            model_pool=a.config.model_pool or "reasoning",
            accepts=a.config.accepts,
            source=registry.source_of(a.name),
            deletable=registry.source_of(a.name) == "personal",
        )
        for a in registry.all()
    ]


@router.delete("/agents/{name}", status_code=204)
async def delete_agent(name: str) -> None:
    """Delete a personal agent and the future work assigned to it.

    Stored schedules are removed and pending jobs are cancelled. Completed and
    otherwise terminal jobs remain as history; running work is allowed to
    finish because marking it cancelled would not stop its in-flight task.
    """
    registry = _get_agent_registry()
    if name not in registry.names():
        raise HTTPException(status_code=404, detail=f"Agent {name!r} was not found")
    if registry.source_of(name) != "personal":
        raise HTTPException(status_code=403, detail="Built-in agents cannot be deleted")

    # Remove future work before the filesystem entry. If either store is
    # temporarily unavailable, the agent remains and a retry can finish the
    # cascade instead of leaving schedules that point at a missing entity.
    await _get_cron_store().remove_for_agent(name)
    processor = _get_job_processor()
    pending = await processor.list_jobs(status=JobStatus.PENDING, limit=1_000_000)
    await asyncio.gather(*(processor.cancel(job.job_id) for job in pending if job.agent == name))
    if not registry.remove_personal(name):
        raise HTTPException(status_code=404, detail=f"Personal agent {name!r} was not found")


@router.post("/agent/run", response_model=TaskResponse, status_code=202)
async def run_agent(request: AgentRunRequest) -> TaskResponse:
    """Manually trigger a specific agent - runs that agent directly, not the planner."""
    registry = _get_agent_registry()
    try:
        registry.get(request.agent)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agent {request.agent!r}. Available: {sorted(registry.names())}",
        ) from None
    return await _get_orchestrator().submit_task(
        TaskRequest(prompt=request.task, forced_agent=request.agent, context=request.context or "")
    )


class AgentCreateRequest(BaseModel):
    name: str
    domain: str
    description: str = ""
    model_pool: str = "fast_cheap"
    accepts: list[str] = []


class AgentCreateResponse(BaseModel):
    name: str
    system_prompt: str


@router.post("/agent/create", response_model=AgentCreateResponse, status_code=201)
async def create_agent(body: AgentCreateRequest) -> AgentCreateResponse:
    """Generate a system prompt for a new agent via the LLM.

    The caller (CLI) is responsible for writing the files to disk.
    """
    router_obj = _get_inference_router()
    prompt = load_prompt("prompts/agent_author.md").format(
        name=body.name,
        domain=body.domain,
        description=body.description or "A domain specialist.",
        model_pool=body.model_pool,
        accepts=", ".join(body.accepts) if body.accepts else "any",
    )

    result = await router_obj.complete(
        CompletionRequest(
            prompt=prompt,
            priority=PoolPriority.MEDIUM,
            component=f"agent_create:{body.name}",
        )
    )
    return AgentCreateResponse(name=body.name, system_prompt=result.text)
