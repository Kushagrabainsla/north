"""FastAPI APIRouter for all Orchestrator endpoints.

See README Section 6.8 and docs/CODING_STYLE.md Sections 12.1-12.4.
"""

from __future__ import annotations

import datetime
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents.registry import AgentRegistry
from config.strategy import NorthSettings, StrategyMode
from context.base import ContextStore
from context.injection import ContextInjector
from context.models import ContextDocument
from inference.base import InferenceRouter
from inference.models import CompletionRequest, CostSummary, PoolPriority, TranscriptionRequest
from jobs.base import JobProcessor
from jobs.cron_store import UserCronStore
from jobs.models import Job, JobPriority, JobStatus, JobType
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerEntry, LedgerSource
from orchestrator.models import TaskRequest, TaskResponse
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream import EventStreamManager
from tools.confidence import ConfidenceTracker
from utils.ids import generate_id
from utils.security import load_secret, verify_request_secret
from utils.time import utcnow

router = APIRouter(
    prefix="/orchestrator",
    tags=["orchestrator"],
    dependencies=[Depends(verify_request_secret)],
)

# Unauthenticated router — only hosts /health so Docker / load-balancer probes
# don't need the API secret.
health_router = APIRouter(tags=["health"])


@health_router.get("/health", include_in_schema=True)
async def health_check() -> dict:
    """Liveness probe — returns 200 {"status": "ok"} with no authentication.

    Used by Docker HEALTHCHECK and any upstream load-balancer probe.
    """
    return {"status": "ok"}

# Module-level singletons injected by app.py at startup
_orchestrator: Orchestrator | None = None
_stream_manager: EventStreamManager | None = None
_ledger: LedgerWriter | None = None
_agent_registry: AgentRegistry | None = None
_context_store: ContextStore | None = None
_context_injector: ContextInjector | None = None
_job_processor: JobProcessor | None = None
_inference_router: InferenceRouter | None = None
_confidence_tracker: ConfidenceTracker | None = None
_cron_store: UserCronStore | None = None
_north_settings: NorthSettings | None = None


def configure(
    orchestrator: Orchestrator,
    stream_manager: EventStreamManager,
    ledger: LedgerWriter,
    agent_registry: AgentRegistry,
    context_store: ContextStore,
    context_injector: ContextInjector,
    job_processor: JobProcessor,
    inference_router: InferenceRouter,
    confidence_tracker: ConfidenceTracker,
    cron_store: UserCronStore | None = None,
    north_settings: NorthSettings | None = None,
) -> None:
    """Wire the singletons used by every route. Called once in app lifespan."""
    global _orchestrator, _stream_manager, _ledger, _agent_registry
    global _context_store, _context_injector, _job_processor
    global _inference_router, _confidence_tracker, _cron_store, _north_settings
    _orchestrator = orchestrator
    _stream_manager = stream_manager
    _ledger = ledger
    _agent_registry = agent_registry
    _context_store = context_store
    _context_injector = context_injector
    _job_processor = job_processor
    _inference_router = inference_router
    _confidence_tracker = confidence_tracker
    _cron_store = cron_store
    _north_settings = north_settings


def _get_orchestrator() -> Orchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not configured")
    return _orchestrator


def _get_stream_manager() -> EventStreamManager:
    if _stream_manager is None:
        raise RuntimeError("EventStreamManager not configured")
    return _stream_manager


def _get_ledger() -> LedgerWriter:
    if _ledger is None:
        raise RuntimeError("LedgerWriter not configured")
    return _ledger


def _get_agent_registry() -> AgentRegistry:
    if _agent_registry is None:
        raise RuntimeError("AgentRegistry not configured")
    return _agent_registry


def _get_context_store() -> ContextStore:
    if _context_store is None:
        raise RuntimeError("ContextStore not configured")
    return _context_store


def _get_context_injector() -> ContextInjector:
    if _context_injector is None:
        raise RuntimeError("ContextInjector not configured")
    return _context_injector


def _get_job_processor() -> JobProcessor:
    if _job_processor is None:
        raise RuntimeError("JobProcessor not configured")
    return _job_processor


def _get_inference_router() -> InferenceRouter:
    if _inference_router is None:
        raise RuntimeError("InferenceRouter not configured")
    return _inference_router


def _get_confidence_tracker() -> ConfidenceTracker:
    if _confidence_tracker is None:
        raise RuntimeError("ConfidenceTracker not configured")
    return _confidence_tracker


# ── Transcription endpoint ────────────────────────────────────────────────────

class TranscriptionOut(BaseModel):
    text: str
    model_used: str
    cost_usd: float


@router.post("/transcribe", response_model=TranscriptionOut)
async def transcribe_audio(request: Request) -> TranscriptionOut:
    """Transcribe raw audio bytes (WAV/MP3) via OpenRouter Whisper.

    The request body must be the raw audio file bytes. The Content-Type
    header should be audio/wav or audio/mpeg.
    """
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Empty audio body.")

    result = await _get_inference_router().transcribe(
        TranscriptionRequest(audio=audio_bytes, component="perception")
    )
    return TranscriptionOut(
        text=result.text,
        model_used=result.model_used,
        cost_usd=result.cost_usd,
    )


# ── Task endpoints ────────────────────────────────────────────────────────────

@router.post("/task", response_model=TaskResponse, status_code=202)
async def submit_task(request: Request) -> TaskResponse:
    """Submit a new task for processing. Accepts JSON or form-encoded bodies."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        task_req = TaskRequest(prompt=str(form.get("prompt", "")))
    else:
        body = await request.json()
        task_req = TaskRequest(**body)
    return await _get_orchestrator().submit_task(task_req)


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks() -> list[TaskResponse]:
    """List all currently pending tasks."""
    return await _get_orchestrator().list_active_tasks()


@router.get("/task/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str) -> TaskResponse:
    """Get the status and most recent output for a specific task."""
    result = await _get_orchestrator().get_task(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return result


@router.delete("/task/{task_id}", status_code=204)
async def cancel_task(task_id: str) -> None:
    """Cancel a pending task."""
    await _get_orchestrator().cancel_task(task_id)


# ── SSE stream ────────────────────────────────────────────────────────────────

@router.get("/stream/{task_id}")
async def stream_task_events(task_id: str) -> StreamingResponse:
    """Server-Sent Events stream for real-time task progress."""

    async def _event_generator() -> AsyncIterator[str]:
        async for chunk in _get_stream_manager().subscribe(task_id):
            yield chunk

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/stream")
async def stream_global_events() -> StreamingResponse:
    """Global SSE stream — all events across all tasks.

    Used by the TUI to receive a single persistent connection for every task
    without needing to subscribe per task_id.
    """

    async def _global_generator() -> AsyncIterator[str]:
        async for chunk in _get_stream_manager().subscribe_global():
            yield chunk

    return StreamingResponse(
        _global_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Metrics endpoint ──────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics(days: int = 7) -> dict:
    """Return aggregated system performance metrics.

    Query params:
        days: look-back window in days (default 7; max 365)
    """
    days = max(1, min(days, 365))
    return await _get_ledger().get_metrics(days=days)


# ── Ledger endpoints ──────────────────────────────────────────────────────────

_LEDGER_EXCLUDE = {"agent_output", "tools_used"}


@router.get("/ledger", response_model=list[LedgerEntry], response_model_exclude=_LEDGER_EXCLUDE)
async def query_ledger(
    task_id: str | None = None,
    agent: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[LedgerEntry]:
    """Query ledger entries with optional filters."""
    src: LedgerSource | None = None
    if source is not None:
        try:
            src = LedgerSource(source)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown source {source!r}. Valid: {[s.value for s in LedgerSource]}",
            ) from None
    return await _get_ledger().query(
        LedgerFilters(task_id=task_id, agent=agent, source=src, limit=limit)
    )


# ── Agent endpoints ───────────────────────────────────────────────────────────

class AgentInfo(BaseModel):
    name: str
    domain: str
    model_pool: str
    accepts: list[str]


class AgentRunRequest(BaseModel):
    agent: str
    task: str
    context: str | None = None


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents() -> list[AgentInfo]:
    """List all registered domain-specialist agents."""
    return [
        AgentInfo(
            name=a.name,
            domain=a.domain,
            model_pool=a.config.model_pool,
            accepts=a.config.accepts,
        )
        for a in _get_agent_registry().all()
    ]


@router.post("/agent/run", response_model=TaskResponse, status_code=202)
async def run_agent(request: AgentRunRequest) -> TaskResponse:
    """Manually trigger a specific agent by submitting a targeted task."""
    return await _get_orchestrator().submit_task(
        TaskRequest(prompt=f"[{request.agent}] {request.task}")
    )


# ── Context endpoints ─────────────────────────────────────────────────────────

_VALID_DOCS = {d.value.replace(".md", ""): d for d in ContextDocument}


def _resolve_doc(doc: str) -> ContextDocument:
    key = doc.replace(".md", "")
    if key not in _VALID_DOCS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown document {doc!r}. Valid: {list(_VALID_DOCS)}",
        )
    return _VALID_DOCS[key]


class ContextDocOut(BaseModel):
    document: str
    content: str


class ContextWriteRequest(BaseModel):
    content: str


@router.get("/context/{doc}", response_model=ContextDocOut)
async def read_context(doc: str) -> ContextDocOut:
    """Read a context document."""
    document = _resolve_doc(doc)
    content = await _get_context_store().read(document)
    return ContextDocOut(document=document.value, content=content)


@router.put("/context/{doc}", status_code=204)
async def write_context(doc: str, body: ContextWriteRequest) -> None:
    """Overwrite a context document entirely."""
    document = _resolve_doc(doc)
    await _get_context_store().write(document, body.content)


@router.post("/context/add", status_code=202)
async def add_context(
    text: str | None = Form(None),
    url: str | None = Form(None),
    file: UploadFile | None = None,
) -> dict[str, str]:
    """Manual context injection: accepts text, URL, or file upload (multipart form)."""
    injector = _get_context_injector()
    if file is not None:
        content = await file.read()
        doc = await injector.inject_file(file.filename or "upload", content)
        return {"document": doc.value, "source": f"file:{file.filename}"}
    if url:
        doc = await injector.inject_url(url)
        return {"document": doc.value, "source": f"url:{url}"}
    if text:
        doc = await injector.inject_text(text)
        return {"document": doc.value, "source": "text"}
    raise HTTPException(status_code=422, detail="Provide text, url, or a file upload")


# ── Job endpoints ─────────────────────────────────────────────────────────────

class JobOut(BaseModel):
    job_id: str
    type: str
    agent: str
    task: str
    status: str
    priority: int
    scheduled_at: str
    created_at: str | None


class JobCreateRequest(BaseModel):
    agent: str
    task: str
    payload: dict[str, Any] = {}
    priority: int = 2
    scheduled_at: str | None = None


def _job_to_out(j: Job) -> JobOut:
    return JobOut(
        job_id=j.job_id,
        type=j.type.value,
        agent=j.agent,
        task=j.task,
        status=j.status.value,
        priority=int(j.priority),
        scheduled_at=j.scheduled_at.isoformat(),
        created_at=j.created_at.isoformat() if j.created_at else None,
    )


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    status: str | None = None,
    limit: int = 50,
) -> list[JobOut]:
    """List job queue entries, optionally filtered by status."""
    js: JobStatus | None = None
    if status is not None:
        try:
            js = JobStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown status {status!r}. Valid: {[s.value for s in JobStatus]}",
            ) from None
    jobs = await _get_job_processor().list_jobs(status=js, limit=limit)
    return [_job_to_out(j) for j in jobs]


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(body: JobCreateRequest) -> JobOut:
    """Create and enqueue a new job."""
    scheduled = (
        datetime.datetime.fromisoformat(body.scheduled_at)
        if body.scheduled_at
        else utcnow()
    )
    job = Job(
        job_id=generate_id(),
        type=JobType.ASYNC,
        agent=body.agent,
        task=body.task,
        payload=body.payload,
        priority=JobPriority(body.priority),
        scheduled_at=scheduled,
    )
    await _get_job_processor().enqueue(job)
    return _job_to_out(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(job_id: str) -> None:
    """Cancel a pending or running job."""
    await _get_job_processor().cancel(job_id)


# ── Cron endpoints ────────────────────────────────────────────────────────────

def _get_cron_store() -> UserCronStore:
    if _cron_store is None:
        raise RuntimeError("CronStore not configured")
    return _cron_store


class CronEntryOut(BaseModel):
    name: str
    agent: str
    task: str
    hour: int
    minute: int
    weekday: int | None


class CronEntryCreate(BaseModel):
    name: str
    agent: str = "general"
    task: str
    hour: int
    minute: int = 0
    weekday: int | None = None


@router.get("/cron", response_model=list[CronEntryOut])
async def list_cron_entries() -> list[CronEntryOut]:
    """List user-defined recurring schedules."""
    entries = await _get_cron_store().list()
    return [CronEntryOut(**e) for e in entries]


@router.post("/cron", response_model=CronEntryOut, status_code=201)
async def create_cron_entry(body: CronEntryCreate) -> CronEntryOut:
    """Add a new recurring schedule."""
    if not (0 <= body.hour <= 23):
        raise HTTPException(status_code=422, detail="hour must be 0-23")
    if not (0 <= body.minute <= 59):
        raise HTTPException(status_code=422, detail="minute must be 0-59")
    if body.weekday is not None and not (0 <= body.weekday <= 6):
        raise HTTPException(status_code=422, detail="weekday must be 0-6 or null")
    await _get_cron_store().add(
        name=body.name,
        agent=body.agent,
        task=body.task,
        hour=body.hour,
        minute=body.minute,
        weekday=body.weekday,
    )
    return CronEntryOut(**body.model_dump())


@router.delete("/cron/{name}", status_code=204)
async def delete_cron_entry(name: str) -> None:
    """Remove a user-defined recurring schedule by name."""
    await _get_cron_store().remove(name)


# ── Inference endpoints ───────────────────────────────────────────────────────

@router.get("/inference/costs", response_model=CostSummary)
async def inference_costs(
    period: str = "week",
    agent: str | None = None,
) -> CostSummary:
    """Aggregated inference costs over a period (day/week/month)."""
    now = utcnow()
    days = {"day": 1, "week": 7, "month": 30}.get(period, 7)
    since = now - datetime.timedelta(days=days)

    entries = await _get_ledger().query(
        LedgerFilters(
            source=LedgerSource.INFERENCE_ROUTER,
            agent=agent,
            since=since,
            limit=10000,
        )
    )

    total = 0.0
    by_component: dict[str, float] = {}
    by_model: dict[str, float] = {}

    for e in entries:
        cost = e.cost_usd or 0.0
        total += cost
        component = e.agent or "unknown"
        by_component[component] = by_component.get(component, 0.0) + cost
        model = e.model_used or "unknown"
        by_model[model] = by_model.get(model, 0.0) + cost

    return CostSummary(
        period=period,
        total_cost_usd=round(total, 6),
        by_component={k: round(v, 6) for k, v in by_component.items()},
        by_model={k: round(v, 6) for k, v in by_model.items()},
    )


class ModelPoolOut(BaseModel):
    name: str
    models: list[str]


@router.get("/inference/models", response_model=dict[str, ModelPoolOut])
async def inference_models() -> dict[str, ModelPoolOut]:
    """Current model pool state."""
    pools = _get_inference_router().current_pools()
    return {name: ModelPoolOut(name=pool.name, models=pool.models) for name, pool in pools.items()}


# ── Tools confidence endpoint ─────────────────────────────────────────────────

class ToolConfidenceOut(BaseModel):
    agent: str
    tool: str
    confidence: float


@router.get("/tools/confidence", response_model=list[ToolConfidenceOut])
async def tool_confidence(agent: str | None = None) -> list[ToolConfidenceOut]:
    """Tool confidence scores, optionally filtered by agent."""
    tracker = _get_confidence_tracker()
    if agent is not None:
        scores = await tracker.scores_for_agent(agent)
        return [ToolConfidenceOut(agent=agent, tool=t, confidence=c) for t, c in scores]

    results: list[ToolConfidenceOut] = []
    for a in _get_agent_registry().names():
        scores = await tracker.scores_for_agent(a)
        results.extend(ToolConfidenceOut(agent=a, tool=t, confidence=c) for t, c in scores)
    return results


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


# ── Settings endpoint ────────────────────────────────────────────────────────

class SettingsOut(BaseModel):
    strategy: str


class SettingsUpdate(BaseModel):
    strategy: str


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    """Return current user settings."""
    mode = _north_settings.strategy.value if _north_settings else "cruise"
    return SettingsOut(strategy=mode)


@router.post("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate) -> SettingsOut:
    """Update user settings directly (alternative to natural language)."""
    try:
        mode = StrategyMode(body.strategy)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown strategy {body.strategy!r}. Valid: eco, cruise, sport",
        ) from None
    if _north_settings is not None:
        _north_settings.set_strategy(mode)
    return SettingsOut(strategy=mode.value)


# ── Webhook endpoint ─────────────────────────────────────────────────────────
#
# External services (GitHub, calendar, email) POST here to trigger agent tasks.
# Authentication: pass the shared north secret in the X-Webhook-Secret header.
# Body (JSON): { "prompt": "...", "context": "..." }
# The source name becomes a prompt prefix so the classifier can route correctly.

webhook_router = APIRouter(
    prefix="/orchestrator",
    tags=["webhooks"],
    # No verify_request_secret dependency — we validate manually below to give
    # a clear 401 rather than the generic 403 from the cookie-based mechanism.
)


@webhook_router.post("/webhooks/{source}", status_code=202)
async def receive_webhook(source: str, request: Request) -> dict:
    """Receive an external event and submit it as a task.

    The ``source`` path parameter identifies the origin (e.g. ``gmail``,
    ``github``, ``calendar``).  The request body must be JSON with at least
    a ``prompt`` key.  Optionally include ``context`` for additional facts
    that should be injected as task context.

    Authentication is via the ``X-Webhook-Secret`` header — same secret as
    the rest of the API.
    """
    secret = load_secret()
    if not secrets.compare_digest(request.headers.get("X-Webhook-Secret", ""), secret):
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
    task_id: str
    agent: str
    decision: str
    chosen_option: str = ""


@router.post("/approval/respond", status_code=204)
async def respond_approval(body: ApprovalResponse) -> None:
    """Receive an approval decision from the notification callback server or Web UI."""
    await _get_orchestrator().respond_approval(
        card_id=body.card_id,
        task_id=body.task_id,
        agent=body.agent,
        decision=body.decision,
        chosen_option=body.chosen_option,
    )
