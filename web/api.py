"""REST endpoints used by the local North browser interface."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bootstrap.onboarding import _discover_files, _load_progress, run_bootstrap_if_needed
from inference.registry import PROVIDER_DEFINITIONS
from ledger.base import LedgerFilters
from orchestrator.models import TaskRequest
from utils.security import WEB_SESSION_COOKIE, issue_web_session, verify_api_access

from .conversations import ConversationStore, Turn

session_router = APIRouter(prefix="/web", tags=["web"])
router = APIRouter(prefix="/web/api", tags=["web"], dependencies=[Depends(verify_api_access)])

_orchestrator = None
_ledger = None
_agent_registry = None
_job_processor = None
_cron_store = None
_approval_store = None
_north_settings = None
_agent_run_store = None
_conversation_store: ConversationStore | None = None
_north_home: Path | None = None
_fact_store = None
_inference_router = None
_bootstrap_task: asyncio.Task | None = None


def configure(
    *,
    orchestrator,
    ledger,
    agent_registry,
    job_processor,
    cron_store,
    approval_store,
    north_settings,
    agent_run_store,
    north_home: Path,
    fact_store=None,
    inference_router=None,
) -> None:
    global _orchestrator, _ledger, _agent_registry, _job_processor, _cron_store
    global _approval_store, _north_settings, _agent_run_store, _conversation_store, _north_home
    global _fact_store, _inference_router
    _orchestrator = orchestrator
    _ledger = ledger
    _agent_registry = agent_registry
    _job_processor = job_processor
    _cron_store = cron_store
    _approval_store = approval_store
    _north_settings = north_settings
    _agent_run_store = agent_run_store
    _north_home = north_home
    _fact_store = fact_store
    _inference_router = inference_router
    _conversation_store = ConversationStore(north_home / "web.db")


def _require(value, name: str):
    if value is None:
        raise RuntimeError(f"{name} not configured")
    return value


@session_router.post("/session")
async def create_local_session(request: Request, response: Response) -> dict[str, Any]:
    """Silently establish a browser session for a loopback-hosted app."""
    if (request.url.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        raise HTTPException(status_code=403, detail="The North web interface is local-only.")
    session = issue_web_session()
    response.set_cookie(
        WEB_SESSION_COOKIE,
        session.token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=12 * 60 * 60,
        path="/",
    )
    return {"csrf": session.csrf, "expires_at": session.expires_at}


class ConversationCreate(BaseModel):
    title: str = Field(default="New chat", max_length=160)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    pinned: bool | None = None
    archived: bool | None = None


class TurnCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_768)


def _conversation_payload(conversation) -> dict[str, Any]:
    return asdict(conversation)


def _entry_payload(entry) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude={"agent_output"})


async def _task_detail(task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    orchestrator = _require(_orchestrator, "Orchestrator")
    ledger = _require(_ledger, "Ledger")
    task, entries, runs = await asyncio.gather(
        orchestrator.get_task(task_id),
        ledger.query(LedgerFilters(task_id=task_id, limit=300, order_asc=True)),
        _require(_agent_run_store, "AgentRunStore").list_for_task(task_id),
    )
    provider_by_model: dict[str, str] = {}
    if _inference_router is not None:
        for pool in _inference_router.current_pools().values():
            for model in pool.models:
                provider_by_model[model.id] = model.provider
    output = ""
    for entry in reversed(entries):
        if entry.action == "agent_completed" and entry.output:
            output = entry.output
            break
    if not output:
        for entry in reversed(entries):
            if entry.output:
                output = entry.output
                break
    return {
        "task": task.model_dump(mode="json") if task else {"task_id": task_id, "status": "unknown"},
        "output": output,
        "entries": [_entry_payload(entry) for entry in entries],
        "runs": [
            {
                **run.__dict__,
                "started_at": run.started_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "models_used": list(run.models_used),
                "providers_used": sorted({provider_by_model.get(model, "unknown") for model in run.models_used}),
                "skills": list(run.skills),
            }
            for run in runs
        ],
    }


async def _turn_payload(turn: Turn) -> dict[str, Any]:
    return {**asdict(turn), "detail": await _task_detail(turn.task_id)}


@router.get("/conversations")
async def list_conversations(q: str = "", archived: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    conversations = await _require(_conversation_store, "ConversationStore").list(
        query=q, archived=archived, limit=limit
    )
    return [_conversation_payload(conversation) for conversation in conversations]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate) -> dict[str, Any]:
    conversation = await _require(_conversation_store, "ConversationStore").create(body.title)
    return _conversation_payload(conversation)


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdate) -> dict[str, Any]:
    conversation = await _require(_conversation_store, "ConversationStore").update(
        conversation_id,
        title=body.title,
        pinned=body.pinned,
        archived=body.archived,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _conversation_payload(conversation)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    store = _require(_conversation_store, "ConversationStore")
    conversation = await store.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    turns = await store.turns(conversation_id)
    payloads = await asyncio.gather(*[_turn_payload(turn) for turn in turns])
    return {**_conversation_payload(conversation), "turns": payloads}


@router.post("/conversations/{conversation_id}/turns", status_code=202)
async def create_turn(conversation_id: str, body: TurnCreate) -> dict[str, Any]:
    store = _require(_conversation_store, "ConversationStore")
    try:
        turn = await store.add_turn(conversation_id, body.prompt)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    previous = await store.turns(conversation_id)
    context_parts: list[str] = []
    for item in previous[-6:-1]:
        detail = await _task_detail(item.task_id)
        answer = detail.get("output", "") if detail else ""
        context_parts.append(f"User: {item.prompt}\nNorth: {answer[:4000]}")
    context = "## Recent conversation\n" + "\n\n".join(context_parts) if context_parts else ""
    task = await _require(_orchestrator, "Orchestrator").submit_task(
        TaskRequest(
            prompt=body.prompt,
            context=context,
            idempotency_key=f"web:{conversation_id}:{turn.id}",
        )
    )
    await store.attach_task(turn.id, task.task_id)
    return {**asdict(turn), "task_id": task.task_id, "detail": {"task": task.model_dump(mode="json")}}


@router.get("/tasks/{task_id}")
async def web_task_detail(task_id: str) -> dict[str, Any]:
    detail = await _task_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return detail


@router.get("/approvals")
async def approvals(limit: int = 100) -> list[dict[str, Any]]:
    return [card.model_dump(mode="json") for card in _require(_approval_store, "ApprovalStore").all(limit)]


@router.get("/system")
async def system_overview() -> dict[str, Any]:
    settings = _require(_north_settings, "North settings")
    providers = []
    for definition in PROVIDER_DEFINITIONS:
        credential = definition.resolve_credential(settings)
        providers.append({
            "id": definition.id,
            "name": definition.display_name,
            "description": definition.description,
            "auth_kind": definition.auth_kind.value,
            "configured": definition.is_configured(settings),
            "env_key": definition.env_key,
            "setup_url": definition.setup_url,
            "credential_hint": (
                credential[:4] + "…" + credential[-4:]
                if len(credential) > 8
                else ("configured" if credential else "")
            ),
        })
    home = _require(_north_home, "North home")
    progress = _load_progress(home) or []
    candidates = [str(path.resolve()) for path in _discover_files()]
    marker = home / ".bootstrapped"
    completed = progress or ([{"path": path, "status": "completed"} for path in candidates] if marker.exists() else [])
    return {
        "providers": providers,
        "settings": {"power": settings.power.value, "autonomy": settings.autonomy.value},
        "bootstrap": {
            "status": "complete" if marker.exists() else ("in_progress" if progress else "not_started"),
            "completed": completed,
            "candidates": candidates,
            "candidate_count": len(candidates),
        },
    }


@router.get("/bootstrap")
async def bootstrap_status() -> dict[str, Any]:
    return (await system_overview())["bootstrap"]


@router.get("/memory/facts")
async def memory_facts() -> list[dict[str, Any]]:
    """Return durable facts for the Memory view without exposing embeddings."""
    if _fact_store is None:
        return []
    return await _fact_store.all_facts()


class BootstrapRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=25)


@router.post("/bootstrap", status_code=202)
async def start_bootstrap(body: BootstrapRequest) -> dict[str, Any]:
    global _bootstrap_task
    if _fact_store is None or _inference_router is None:
        raise HTTPException(status_code=503, detail="Bootstrap dependencies are not ready")
    if _bootstrap_task and not _bootstrap_task.done():
        return {"status": "in_progress"}
    candidates = {str(path.resolve()) for path in _discover_files()}
    selected = {str(Path(path).expanduser().resolve()) for path in body.paths}
    if selected and not selected.issubset(candidates):
        raise HTTPException(status_code=400, detail="One or more selected files are not eligible bootstrap sources")
    _bootstrap_task = asyncio.create_task(
        run_bootstrap_if_needed(
            _fact_store,
            _inference_router,
            _require(_north_home, "North home"),
            selected_paths=selected or None,
        )
    )
    return {"status": "started", "selected": len(selected) or len(candidates)}


class ProviderCredentialUpdate(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


@router.post("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderCredentialUpdate) -> dict[str, Any]:
    definition = next((item for item in PROVIDER_DEFINITIONS if item.id == provider_id), None)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if not definition.env_key:
        raise HTTPException(status_code=400, detail="This provider uses browser authentication; use the CLI login flow")
    home = _require(_north_home, "North home")
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{definition.env_key}="
    replacement = f"{definition.env_key}={body.api_key.strip()}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    os.environ[definition.env_key] = body.api_key.strip()
    from config.runtime import get_runtime
    from config.settings import reload_settings, settings
    from inference.factory import build_router

    reload_settings()
    runtime = get_runtime()
    if runtime is not None:
        new_router = build_router(
            openrouter_api_key=settings.openrouter_api_key,
            north_settings=runtime.north_settings,
            groq_api_key=settings.groq_api_key,
            gemini_api_key=settings.gemini_api_key,
            opencode_zen_api_key=settings.opencode_zen_api_key,
            provider_settings=settings,
            confidence_tracker=runtime.confidence_tracker,
            cooldowns_path=settings.north_home / "cooldowns.json",
        )
        runtime.cost_tracker.set_inner(new_router)
    return {"id": definition.id, "configured": True, "credential_hint": body.api_key[:4] + "…" + body.api_key[-4:]}


def _allowed_output_roots() -> list[Path]:
    home = _require(_north_home, "North home")
    return [home / name for name in ("news", "notes", "wellness")]


def _artifact_id(path: Path) -> str:
    home = _require(_north_home, "North home")
    relative = str(path.relative_to(home))
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


def _artifact_path(artifact_id: str) -> Path:
    try:
        padded = artifact_id + "=" * (-len(artifact_id) % 4)
        relative = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    home = _require(_north_home, "North home").resolve()
    path = (home / relative).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in _allowed_output_roots()):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return path


def _list_artifacts_sync(limit: int) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for root in _allowed_output_roots():
        if root.exists():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    result = []
    for path in paths[: max(1, min(limit, 500))]:
        stat = path.stat()
        result.append(
            {
                "id": _artifact_id(path),
                "name": path.name,
                "kind": path.parent.name,
                "media_type": mimetypes.guess_type(path.name)[0] or "text/plain",
                "size": stat.st_size,
                "updated_at": stat.st_mtime,
            }
        )
    return result


@router.get("/artifacts")
async def list_artifacts(limit: int = 100) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_artifacts_sync, limit)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict[str, Any]:
    path = _artifact_path(artifact_id)
    if path.stat().st_size > 2_000_000:
        raise HTTPException(status_code=413, detail="Artifact is too large to preview")
    content = await asyncio.to_thread(path.read_text, "utf-8", "replace")
    return {
        "id": artifact_id,
        "name": path.name,
        "kind": path.parent.name,
        "media_type": mimetypes.guess_type(path.name)[0] or "text/plain",
        "content": content,
    }


@router.get("/dashboard")
async def dashboard() -> dict[str, Any]:
    active, jobs, cron, metrics, ledger_entries, conversations, artifacts = await asyncio.gather(
        _require(_orchestrator, "Orchestrator").list_active_tasks(),
        _require(_job_processor, "JobProcessor").list_jobs(limit=8),
        _require(_cron_store, "CronStore").list(),
        _require(_ledger, "Ledger").get_metrics(days=7),
        _require(_ledger, "Ledger").query(LedgerFilters(limit=12)),
        _require(_conversation_store, "ConversationStore").list(limit=6),
        list_artifacts(limit=6),
    )
    agents = _require(_agent_registry, "AgentRegistry").all()
    settings = _require(_north_settings, "NorthSettings")
    return {
        "system": {"status": "online", "power": settings.power.value, "autonomy": settings.autonomy.value},
        "attention": [card.model_dump(mode="json") for card in _require(_approval_store, "ApprovalStore").pending()],
        "active_tasks": [task.model_dump(mode="json") for task in active],
        "conversations": [_conversation_payload(item) for item in conversations],
        "agents": [
            {"name": agent.name, "domain": agent.domain, "model_pool": agent.config.model_pool or "reasoning"}
            for agent in agents
        ],
        "jobs": [
            {
                "job_id": job.job_id,
                "agent": job.agent,
                "task": job.task,
                "status": job.status.value,
                "scheduled_at": job.scheduled_at.isoformat(),
            }
            for job in jobs
        ],
        "cron": cron,
        "metrics": metrics,
        "activity": [_entry_payload(entry) for entry in ledger_entries],
        "artifacts": artifacts,
    }
