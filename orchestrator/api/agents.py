"""Agent registry: list, run one directly, scaffold a new one."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from agents.exceptions import AgentNotFoundError
from inference.models import CompletionRequest, PoolPriority
from orchestrator.api.deps import _get_agent_registry, _get_inference_router, _get_orchestrator, router
from orchestrator.models import TaskRequest, TaskResponse
from utils.prompts import load_prompt


class AgentInfo(BaseModel):
    name: str
    domain: str
    model_pool: str = "reasoning"
    accepts: list[str] = []


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
    return [
        AgentInfo(
            name=a.name,
            domain=a.domain,
            model_pool=a.config.model_pool or "reasoning",
            accepts=a.config.accepts,
        )
        for a in _get_agent_registry().all()
    ]


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
    tools: list[str] = []
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
        tools=", ".join(body.tools) if body.tools else "none specified",
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


