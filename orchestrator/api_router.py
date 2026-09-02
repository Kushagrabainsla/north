"""Orchestrator API routes not yet moved into `orchestrator/api/`.

Routes are being split per area (CODING_STYLE §12.4). Areas already moved live
in `orchestrator/api/`; this module keeps the rest and re-exports the package's
router objects so importers keep working during the move.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator

from agents.exceptions import AgentNotFoundError
from approval.mode import parse_approval_mode
from config.strategy import NorthSettings, StrategyMode
from inference.models import CompletionRequest, PoolPriority, TranscriptionRequest
from jobs.models import Job, JobPriority, JobStatus, JobType
from ledger.base import LedgerFilters
from ledger.models import LedgerEntry, LedgerSource
from memory.gateway import LocalMemoryGateway
from memory.models import ContextDocument
from orchestrator.api import health_check, health_router, router  # noqa: F401  (re-exported)
from orchestrator.api.deps import (
    _get_agent_registry,
    _get_agent_run_store,
    _get_context_injector,
    _get_context_store,
    _get_cron_store,
    _get_inference_router,
    _get_job_processor,
    _get_ledger,
    _get_orchestrator,
    configure,  # noqa: F401  (re-exported)
)
from orchestrator.api_context import current_services
from orchestrator.models import TaskRequest, TaskResponse
from utils.ids import generate_id
from utils.security import verify_secret
from utils.time import utcnow

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
    cancelled = await _get_orchestrator().cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not in flight - nothing to cancel.")


@router.post("/task/{task_id}/pause")
async def pause_task(task_id: str) -> dict[str, str]:
    """Pause a running task. The task stops but can be resumed later."""
    paused = await _get_orchestrator().pause_task(task_id)
    if not paused:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not in flight - nothing to pause.")
    return {"status": "paused", "task_id": task_id}


@router.post("/task/{task_id}/resume")
async def resume_task(task_id: str) -> dict[str, str]:
    """Resume a previously paused task."""
    resumed = await _get_orchestrator().resume_paused_task(task_id)
    if not resumed:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} is not paused or already in flight.")
    return {"status": "resumed", "task_id": task_id}


@router.post("/cancel-all")
async def cancel_all() -> dict[str, int]:
    """Stop everything in flight: cancel all active tasks and all pending jobs."""
    tasks_cancelled = await _get_orchestrator().cancel_all_tasks()
    processor = _get_job_processor()
    jobs_cancelled = 0
    for job in await processor.list_jobs(status=JobStatus.PENDING, limit=1000):
        await processor.cancel(job.job_id)
        jobs_cancelled += 1
    return {"tasks_cancelled": tasks_cancelled, "jobs_cancelled": jobs_cancelled}


@router.post("/cancel/{target_id}")
async def cancel_any(target_id: str) -> dict[str, str]:
    """Cancel one thing by id, whether it's an active task or a pending/running job."""
    if await _get_orchestrator().cancel_task(target_id):
        return {"cancelled": "task", "id": target_id}
    processor = _get_job_processor()
    job = await processor.get(target_id)
    if job is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        await processor.cancel(target_id)
        return {"cancelled": "job", "id": target_id}
    raise HTTPException(
        status_code=404, detail=f"{target_id!r} is not an active task or a pending/running job."
    )


# ── Ledger endpoints ──────────────────────────────────────────────────────────

# Bulk fields the ledger listing never needs. frozenset so a caller cannot
# mutate the exclusion set that every /ledger response is rendered through.
_LEDGER_EXCLUDE: frozenset[str] = frozenset({"agent_output", "tools_used"})


@router.get("/ledger", response_model=list[LedgerEntry], response_model_exclude=_LEDGER_EXCLUDE)
async def query_ledger(
    task_id: str | None = None,
    run_id: str | None = None,
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
        LedgerFilters(task_id=task_id, run_id=run_id, agent=agent, source=src, limit=limit)
    )


class SearchOut(BaseModel):
    entry: LedgerEntry
    rank: float
    snippet: str


@router.get("/ledger/search", response_model=list[SearchOut])
async def search_ledger(
    q: str,
    limit: int = 20,
    agent: str | None = None,
    source: str | None = None,
) -> list[SearchOut]:
    """Full-text search over ledger entries."""
    src: LedgerSource | None = None
    if source is not None:
        try:
            src = LedgerSource(source)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown source {source!r}. Valid: {[s.value for s in LedgerSource]}",
            ) from None
    results = await _get_ledger().search(query=q, limit=limit, agent=agent, source=src)
    return [SearchOut(entry=r.entry, rank=r.rank, snippet=r.snippet) for r in results]


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
    context_store = _get_context_store()
    # soul.md has a shipped persona fallback when no user override exists.
    # Use the same gateway path as agents so the Memory UI shows the effective
    # document rather than an empty editor for a missing override.
    content = (
        await LocalMemoryGateway(context_store).read_persona()
        if document is ContextDocument.SOUL
        else await context_store.read(document)
    )
    return ContextDocOut(document=document.value, content=content)


@router.put("/context/{doc}", status_code=204)
async def write_context(doc: str, body: ContextWriteRequest) -> None:
    """Overwrite a context document entirely."""
    document = _resolve_doc(doc)
    await _get_context_store().write(document, body.content)


@router.delete("/context/{doc}", status_code=204)
async def delete_context(doc: str) -> None:
    """Delete a user-customized context document."""
    document = _resolve_doc(doc)
    delete = getattr(_get_context_store(), "delete", None)
    if delete is None:
        raise HTTPException(status_code=405, detail="This context store does not support deletion")
    await delete(document)


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
    scheduled = datetime.datetime.fromisoformat(body.scheduled_at) if body.scheduled_at else utcnow()
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


# ── Settings endpoint ────────────────────────────────────────────────────────


class SettingsOut(BaseModel):
    power: str
    autonomy: str


class SettingsUpdate(BaseModel):
    # Preferred dial names.
    power: str | None = None
    autonomy: str | None = None


def _settings_out(settings_obj: NorthSettings | None) -> SettingsOut:
    """Render the dials, falling back to the documented defaults when unwired."""
    return SettingsOut(
        power=settings_obj.power.value if settings_obj else "cruise",
        autonomy=settings_obj.autonomy.value if settings_obj else "interactive",
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    """Return current user settings."""
    return _settings_out(current_services().north_settings)


@router.post("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate) -> SettingsOut:
    """Update user settings live (power and/or autonomy). No restart needed."""
    settings_obj = current_services().north_settings
    if body.power is not None:
        try:
            mode = StrategyMode(body.power)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown power {body.power!r}. Valid: eco, cruise, sport",
            ) from None
        if settings_obj is not None:
            settings_obj.set_power(mode)

    if body.autonomy is not None:
        approval_mode = parse_approval_mode(body.autonomy)
        if approval_mode is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown autonomy {body.autonomy!r}. Valid: interactive, auto, autonomous",
            ) from None
        if settings_obj is not None:
            settings_obj.set_autonomy(approval_mode)

    return _settings_out(settings_obj)


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
