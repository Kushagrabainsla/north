"""Inspect the agent-run tree and each run's durable events."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from orchestrator.api.deps import _get_agent_run_store, router


class AgentRunOut(BaseModel):
    run_id: str
    task_id: str
    parent_run_id: str | None
    agent: str
    attempt: int
    status: str
    prompt: str
    workspace: str
    model_pool: str
    delegation_depth: int
    started_at: datetime.datetime
    completed_at: datetime.datetime | None
    duration_ms: int | None
    output: str | None
    summary: str | None
    error: str | None
    models_used: list[str]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    skills: list[dict[str, str]]
    provider_state: dict[str, Any]


@router.get("/tasks/{task_id}/runs", response_model=list[AgentRunOut])
async def task_agent_runs(task_id: str) -> list[AgentRunOut]:
    """Return the durable execution tree for a task."""
    runs = await _get_agent_run_store().list_for_task(task_id)
    return [
        AgentRunOut(
            **{
                **run.__dict__,
                "models_used": list(run.models_used),
                "skills": list(run.skills),
            }
        )
        for run in runs
    ]


@router.get("/runs/{run_id}/events", response_model=list[dict[str, Any]])
async def agent_run_events(run_id: str) -> list[dict[str, Any]]:
    """Return significant, durable events for one agent invocation."""
    if await _get_agent_run_store().get(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return await _get_agent_run_store().list_events(run_id)


