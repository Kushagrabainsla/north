"""Orchestrator API routes not yet moved into `orchestrator/api/`.

Routes are being split per area (CODING_STYLE §12.4). Areas already moved live
in `orchestrator/api/`; this module keeps the rest and re-exports the package's
router objects so importers keep working during the move.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from agents.exceptions import AgentNotFoundError
from inference.models import CompletionRequest, PoolPriority, TranscriptionRequest
from ledger.models import LedgerSource
from orchestrator.api import health_check, health_router, router  # noqa: F401  (re-exported)
from orchestrator.api.deps import (
    _get_agent_registry,
    _get_agent_run_store,
    _get_inference_router,
    _get_orchestrator,
    configure,  # noqa: F401  (re-exported)
)
from orchestrator.models import TaskRequest, TaskResponse
from utils.security import verify_secret

# ── Transcription endpoint ────────────────────────────────────────────────────


class TranscriptionOut(BaseModel):
    text: str
    model_used: str
    cost_usd: float


# 25 MB ≈ 25 minutes of 16-bit 16 kHz WAV - far more than a dictation clip.
# Bounding the read prevents a single request from exhausting memory and
# putting an unbounded payload in front of a paid transcription API.
MAX_TRANSCRIBE_BYTES = 25 * 1024 * 1024


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """Read the request body, rejecting payloads larger than *max_bytes* (413)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Body exceeds {max_bytes} byte limit.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Body exceeds {max_bytes} byte limit.")
    return bytes(body)


@router.post("/transcribe", response_model=TranscriptionOut)
async def transcribe_audio(request: Request) -> TranscriptionOut:
    """Transcribe raw audio bytes (WAV/MP3) via OpenRouter Whisper.

    The request body must be the raw audio file bytes (max 25 MB). The
    Content-Type header should be audio/wav or audio/mpeg.
    """
    audio_bytes = await _read_body_capped(request, MAX_TRANSCRIBE_BYTES)
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Empty audio body.")

    result = await _get_inference_router().transcribe(TranscriptionRequest(audio=audio_bytes, component="perception"))
    return TranscriptionOut(
        text=result.text,
        model_used=result.model_used,
        cost_usd=result.cost_usd,
    )


# ── Agent endpoints ───────────────────────────────────────────────────────────


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


# ── Agent run inspection ──────────────────────────────────────────────────────


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


# ── Agent create endpoint ─────────────────────────────────────────────────────


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
    prompt = (
        f"You are writing the system prompt for a new north AI agent.\n\n"
        f"Agent name: {body.name}\n"
        f"Domain: {body.domain}\n"
        f"Description: {body.description or 'A domain specialist.'}\n"
        f"Model pool: {body.model_pool}\n"
        f"Tools available: {', '.join(body.tools) if body.tools else 'none specified'}\n"
        f"Accepts task types: {', '.join(body.accepts) if body.accepts else 'any'}\n\n"
        f"Write a concise but complete system prompt (200-400 words) that:\n"
        f"1. Defines the agent's role and expertise in the {body.domain} domain\n"
        f"2. Lists what kinds of tasks it handles\n"
        f"3. Describes its reasoning style and output format\n"
        f"4. Mentions the tools it can use\n\n"
        f"Output ONLY the system prompt text, no preamble."
    )

    result = await router_obj.complete(
        CompletionRequest(
            prompt=prompt,
            priority=PoolPriority.MEDIUM,
            component=f"agent_create:{body.name}",
        )
    )
    return AgentCreateResponse(name=body.name, system_prompt=result.text)


# ── Webhook endpoint ─────────────────────────────────────────────────────────
#
# External services (GitHub, calendar, email) POST here to trigger agent tasks.
# Authentication: pass the shared north secret in the X-Webhook-Secret header.
# Body (JSON): { "prompt": "...", "context": "..." }
# The source name becomes a prompt prefix so the classifier can route correctly.

webhook_router = APIRouter(
    prefix="/orchestrator",
    tags=["webhooks"],
    # No verify_request_secret dependency - we validate manually below to give
    # a clear 401 rather than the generic 403 from the cookie-based mechanism.
)


@webhook_router.post("/webhooks/{source}", status_code=202)
async def receive_webhook(source: str, request: Request) -> dict:
    """Receive an external event and submit it as a task.

    The ``source`` path parameter identifies the origin (e.g. ``gmail``,
    ``github``, ``calendar``).  The request body must be JSON with at least
    a ``prompt`` key.  Optionally include ``context`` for additional facts
    that should be injected as task context.

    Authentication is via the ``X-Webhook-Secret`` header - same secret as
    the rest of the API.
    """
    webhook_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or not verify_secret(webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Webhook-Secret header.")

    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt = str(body.get("prompt") or body.get("message") or f"Process incoming {source} event")
    context = str(body.get("context", ""))

    task_req = TaskRequest(
        prompt=f"[webhook:{source}] {prompt}",
        source=LedgerSource.WEBHOOK,
        context=context,
    )

    orch = _get_orchestrator()
    result = await orch.submit_task(task_req)
    return {"task_id": result.task_id, "status": result.status, "source": source}


# ── Approval endpoint ─────────────────────────────────────────────────────────


class ApprovalResponse(BaseModel):
    card_id: str
    decision: str
    chosen_option: str = ""
    # Legacy fields - ignored. The decision binds to the server-issued card:
    # task_id and agent are read from the stored card, never trusted from the client.
    task_id: str = ""
    agent: str = ""


@router.post("/approval/respond", status_code=204)
async def respond_approval(body: ApprovalResponse) -> None:
    """Receive an approval decision from the notification callback server or Web UI.

    The card_id must reference a pending card issued by this server; the
    task/agent identity comes from that card, not the request body.
    """
    try:
        await _get_orchestrator().respond_approval(
            card_id=body.card_id,
            decision=body.decision,
            chosen_option=body.chosen_option,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


class SteerRequest(BaseModel):
    task_id: str = ""
    instruction: str


@router.post("/steer")
async def steer_task(body: SteerRequest) -> dict:
    """Submit an in-flight steering directive to an active task."""
    orch = _get_orchestrator()
    task_id = body.task_id
    if not task_id:
        active = list(orch._active_tasks.keys())
        if not active:
            raise HTTPException(status_code=404, detail="No active task to steer.")
        task_id = active[-1]

    await orch.emit_steer(task_id, body.instruction)
    return {"status": "ok", "task_id": task_id, "instruction": body.instruction}
