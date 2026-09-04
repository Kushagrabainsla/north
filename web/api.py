"""REST endpoints used by the local North browser interface."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bootstrap.onboarding import _discover_files, _load_progress, run_bootstrap_if_needed
from inference.codex_auth import CodexCredentialProvider
from inference.registry import PROVIDER_DEFINITIONS, AuthKind, ProviderDefinition
from ledger.base import LedgerFilters
from orchestrator.api_context import bind_request_services, current_services, merge
from orchestrator.models import TaskRequest
from tools._path import DB_SUFFIXES
from utils.security import WEB_SESSION_COOKIE, issue_web_session, verify_api_access

from .conversations import ConversationStore, Turn

session_router = APIRouter(prefix="/web", tags=["web"], dependencies=[Depends(bind_request_services)])
router = APIRouter(
    prefix="/web/api",
    tags=["web"],
    # Bound first so routes can reach this app's wiring via current_services().
    dependencies=[Depends(bind_request_services), Depends(verify_api_access)],
)

@dataclass
class ProviderAuthSession:
    """Non-secret state for one dashboard-managed browser login."""

    provider_id: str
    state: str = "starting"
    authorization_url: str = ""
    detail: str = "Preparing browser login…"
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class WebRuntime:
    """Per-app runtime state for the web layer.

    Not injected wiring - these track work this app has in flight, so they are
    mutable and live alongside the wiring on ``app.state`` rather than at module
    scope, where two apps would share one bootstrap task and one set of logins.
    """

    bootstrap_task: asyncio.Task | None = None
    auth_sessions: dict[str, ProviderAuthSession] = field(default_factory=dict)



def configure(
    app: FastAPI,
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
    skill_registry=None,
) -> None:
    """Contribute the web layer's wiring to *app*.

    Merges rather than replaces: the orchestrator router configures the same app
    and each owns a different slice (CODING_STYLE §22 - no module-level state).
    """
    merge(
        app,
        orchestrator=orchestrator,
        ledger=ledger,
        agent_registry=agent_registry,
        job_processor=job_processor,
        cron_store=cron_store,
        approval_store=approval_store,
        north_settings=north_settings,
        agent_run_store=agent_run_store,
        north_home=north_home,
        fact_store=fact_store,
        inference_router=inference_router,
        skill_registry=skill_registry,
        conversation_store=ConversationStore(north_home / "web.db"),
        web_runtime=WebRuntime(),
    )


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
    orchestrator = current_services().require("orchestrator")
    ledger = current_services().require("ledger")
    task, entries, runs = await asyncio.gather(
        orchestrator.get_task(task_id),
        ledger.query(LedgerFilters(task_id=task_id, limit=300, order_asc=True)),
        current_services().require("agent_run_store").list_for_task(task_id),
    )
    provider_by_model: dict[str, str] = {}
    if current_services().inference_router is not None:
        for pool in current_services().inference_router.current_pools().values():
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
    conversations = await current_services().require("conversation_store").list(
        query=q, archived=archived, limit=limit
    )
    return [_conversation_payload(conversation) for conversation in conversations]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate) -> dict[str, Any]:
    conversation = await current_services().require("conversation_store").create(body.title)
    return _conversation_payload(conversation)


@router.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdate) -> dict[str, Any]:
    conversation = await current_services().require("conversation_store").update(
        conversation_id,
        title=body.title,
        pinned=body.pinned,
        archived=body.archived,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _conversation_payload(conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str) -> None:
    deleted = await current_services().require("conversation_store").delete(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    store = current_services().require("conversation_store")
    conversation = await store.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    turns = await store.turns(conversation_id)
    payloads = await asyncio.gather(*[_turn_payload(turn) for turn in turns])
    return {**_conversation_payload(conversation), "turns": payloads}


@router.post("/conversations/{conversation_id}/turns", status_code=202)
async def create_turn(conversation_id: str, body: TurnCreate) -> dict[str, Any]:
    store = current_services().require("conversation_store")
    try:
        turn = await store.add_turn(conversation_id, body.prompt)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    previous = await store.turns(conversation_id)
    context_parts: list[str] = []
    for item in previous[-6:-1]:
        detail = await _task_detail(item.task_id)
        # A pending/paused turn has no authoritative final answer yet. Injecting
        # its partial output makes the next task look like a continuation of the
        # previous question and can cause North to answer the wrong turn.
        if not detail or detail.get("task", {}).get("status") != "completed":
            continue
        answer = detail.get("output", "")
        if answer:
            context_parts.append(f"User: {item.prompt}\nNorth: {answer[:4000]}")
    context = "## Recent conversation\n" + "\n\n".join(context_parts) if context_parts else ""
    task = await current_services().require("orchestrator").submit_task(
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
    return [card.model_dump(mode="json") for card in current_services().require("approval_store").all(limit)]


def _bootstrap_overview(home: Path) -> tuple[list, list[str], bool]:
    """Read bootstrap state from disk: (progress, candidate paths, completed?).

    Blocking - `_discover_files()` walks the filesystem, so call via to_thread.
    """
    progress = _load_progress(home) or []
    candidates = [str(path.resolve()) for path in _discover_files()]
    return progress, candidates, (home / ".bootstrapped").exists()


@router.get("/system")
async def system_overview() -> dict[str, Any]:
    settings = current_services().require("north_settings")
    providers = []
    for definition in PROVIDER_DEFINITIONS:
        credential = definition.resolve_credential(settings)
        providers.append(
            {
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
            }
        )
    home = current_services().require("north_home")
    # _discover_files() walks the user's home directory - off-thread so this
    # GET never stalls the event loop (CODING_STYLE §10.3).
    progress, candidates, bootstrapped = await asyncio.to_thread(_bootstrap_overview, home)
    completed = progress or ([{"path": path, "status": "completed"} for path in candidates] if bootstrapped else [])
    return {
        "providers": providers,
        "settings": {"power": settings.power.value, "autonomy": settings.autonomy.value},
        "bootstrap": {
            "status": "complete" if bootstrapped else ("in_progress" if progress else "not_started"),
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
    if current_services().fact_store is None:
        return []
    return await current_services().fact_store.all_facts()


class FactCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="user", max_length=80)


class SkillUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


@router.get("/skills")
async def list_skills() -> list[dict[str, Any]]:
    registry = current_services().require("skill_registry")
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "source": skill.source.value,
            "version": skill.version,
            "status": skill.status,
            "domains": sorted(skill.domains),
        }
        for skill in sorted(registry.all(), key=lambda item: item.name)
    ]


@router.get("/skills/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    registry = current_services().require("skill_registry")
    try:
        skill = registry.get(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    content = await asyncio.to_thread((skill.directory / "SKILL.md").read_text, encoding="utf-8")
    return {"name": skill.name, "content": content, "source": skill.source.value}


@router.put("/skills/{name}")
async def update_skill(name: str, body: SkillUpdate) -> dict[str, Any]:
    from skills.exceptions import SkillParseError
    from skills.parser import parse_skill_document
    from skills.registry import rejection_reason

    registry = current_services().require("skill_registry")
    try:
        skill = registry.get(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        frontmatter, content_body = parse_skill_document(body.content)
    except SkillParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    parsed_name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    reason = rejection_reason(parsed_name, description, content_body, source=skill.source)
    if parsed_name != name or reason:
        raise HTTPException(status_code=422, detail=reason or "Skill name cannot be changed")
    await asyncio.to_thread((skill.directory / "SKILL.md").write_text, body.content, encoding="utf-8")
    registry.reload()
    return await get_skill(name)


@router.post("/memory/facts", status_code=201)
async def create_memory_fact(body: FactCreate) -> dict[str, Any]:
    store = current_services().require("fact_store")
    await store.add_fact(body.content, body.category)
    match = next(
        (fact for fact in await store.all_facts(body.category) if fact["content"] == body.content.strip()),
        None,
    )
    if match is None:
        raise HTTPException(status_code=409, detail="Fact already exists or was rejected")
    return match


@router.patch("/memory/facts/{fact_id}")
async def update_memory_fact(fact_id: str, body: FactCreate) -> dict[str, Any]:
    store = current_services().require("fact_store")
    if not await store.update_fact(fact_id, body.content, body.category):
        raise HTTPException(status_code=404, detail="Fact not found or rejected")
    return next(fact for fact in await store.all_facts() if fact["id"] == fact_id)


@router.delete("/memory/facts/{fact_id}", status_code=204)
async def delete_memory_fact(fact_id: str) -> None:
    if not await current_services().require("fact_store").delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Fact not found")


class BootstrapRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=25)


@router.post("/bootstrap", status_code=202)
async def start_bootstrap(body: BootstrapRequest) -> dict[str, Any]:
    runtime = current_services().require("web_runtime")
    if current_services().fact_store is None or current_services().inference_router is None:
        raise HTTPException(status_code=503, detail="Bootstrap dependencies are not ready")
    if runtime.bootstrap_task and not runtime.bootstrap_task.done():
        return {"status": "in_progress"}
    candidates = {str(path.resolve()) for path in _discover_files()}
    selected = {str(Path(path).expanduser().resolve()) for path in body.paths}
    if selected and not selected.issubset(candidates):
        raise HTTPException(status_code=400, detail="One or more selected files are not eligible bootstrap sources")
    runtime.bootstrap_task = asyncio.create_task(
        run_bootstrap_if_needed(
            current_services().fact_store,
            current_services().inference_router,
            current_services().require("north_home"),
            selected_paths=selected or None,
        )
    )
    return {"status": "started", "selected": len(selected) or len(candidates)}


class ProviderCredentialUpdate(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


def _provider_definition(provider_id: str) -> ProviderDefinition:
    normalized = provider_id.strip().lower().replace("-", "_")
    definition = next((item for item in PROVIDER_DEFINITIONS if item.id == normalized), None)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return definition


async def _refresh_inference_runtime(app: FastAPI) -> None:
    """Rebuild the live router after provider credentials change.

    Takes the app so the rebuilt router replaces the one this app's routes read
    (`merge`), alongside swapping it into the live dependency container.
    """
    from config.runtime import get_runtime
    from config.settings import reload_settings, settings
    from inference.factory import build_router

    reload_settings()
    deps = get_runtime()
    if deps is None:
        return
    new_router = build_router(
        openrouter_api_key=settings.openrouter_api_key,
        north_settings=deps.north_settings,
        groq_api_key=settings.groq_api_key,
        gemini_api_key=settings.gemini_api_key,
        opencode_zen_api_key=settings.opencode_zen_api_key,
        provider_settings=settings,
        confidence_tracker=deps.confidence_tracker,
        cooldowns_path=settings.north_home / "cooldowns.json",
    )
    deps.inference_router = new_router
    deps.cost_tracker.set_inner(new_router)
    merge(app, inference_router=new_router)


def _provider_auth_payload(definition: ProviderDefinition) -> dict[str, Any]:
    credentials = CodexCredentialProvider()
    status = credentials.status()
    session = current_services().require("web_runtime").auth_sessions.get(definition.id)
    state = session.state if session else ("connected" if status.configured else "disconnected")
    detail = session.detail if session else status.detail
    if session and session.state == "connected" and not status.configured:
        state = "disconnected"
        detail = status.detail
    account_hint = (
        f"…{status.account_id[-6:]}" if status.account_id and len(status.account_id) > 6 else status.account_id
    )
    return {
        "provider_id": definition.id,
        "state": state,
        "configured": status.configured,
        "needs_login": status.needs_login,
        "detail": detail,
        "authorization_url": session.authorization_url if session and state in {"starting", "pending"} else "",
        "account_hint": account_hint or "",
        "expires_at": status.expires_at.isoformat() if status.expires_at else None,
    }


@router.post("/providers/{provider_id}/auth")
async def start_provider_auth(provider_id: str, request: Request) -> dict[str, Any]:
    definition = _provider_definition(provider_id)
    if definition.auth_kind is not AuthKind.OAUTH_PKCE or definition.id != "openai_codex":
        raise HTTPException(status_code=400, detail="This provider does not support browser login")

    existing = current_services().require("web_runtime").auth_sessions.get(definition.id)
    if existing and existing.task and not existing.task.done():
        return _provider_auth_payload(definition)

    ready = asyncio.Event()
    session = ProviderAuthSession(provider_id=definition.id)
    current_services().require("web_runtime").auth_sessions[definition.id] = session

    def authorization_ready(url: str) -> None:
        session.authorization_url = url
        session.state = "pending"
        session.detail = "Complete the OpenAI login in the browser window."
        ready.set()

    credentials = CodexCredentialProvider(authorization_callback=authorization_ready)
    # Captured now: the background login outlives this request, so it cannot
    # read the app off a request that has already finished.
    app = request.app

    async def run_login() -> None:
        try:
            status = await credentials.login(open_browser=False)
            session.state = "connected"
            session.detail = status.detail or "Logged in"
            await _refresh_inference_runtime(app)
        except asyncio.CancelledError:
            session.state = "cancelled"
            session.detail = "Login cancelled"
            raise
        except Exception as exc:
            session.state = "error"
            session.detail = str(exc)
        finally:
            ready.set()

    session.task = asyncio.create_task(run_login(), name=f"provider-auth-{definition.id}")
    try:
        await asyncio.wait_for(ready.wait(), timeout=3)
    except TimeoutError:
        session.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.task
        session.state = "error"
        session.detail = "Browser login could not be started"
    return _provider_auth_payload(definition)


@router.get("/providers/{provider_id}/auth")
async def provider_auth_status(provider_id: str) -> dict[str, Any]:
    definition = _provider_definition(provider_id)
    if definition.auth_kind is not AuthKind.OAUTH_PKCE or definition.id != "openai_codex":
        raise HTTPException(status_code=400, detail="This provider does not support browser login")
    return _provider_auth_payload(definition)


@router.delete("/providers/{provider_id}/auth")
async def logout_provider(provider_id: str, request: Request) -> dict[str, Any]:
    definition = _provider_definition(provider_id)
    if definition.auth_kind is not AuthKind.OAUTH_PKCE or definition.id != "openai_codex":
        raise HTTPException(status_code=400, detail="This provider does not support browser login")
    session = current_services().require("web_runtime").auth_sessions.pop(definition.id, None)
    if session and session.task and not session.task.done():
        session.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session.task
    await CodexCredentialProvider().logout()
    await _refresh_inference_runtime(request.app)
    return _provider_auth_payload(definition)


def _write_env_key(home: Path, env_key: str, value: str) -> None:
    """Upsert `env_key=value` in ~/.north/.env with private permissions.

    Blocking - call via to_thread so a request handler never stalls the loop.
    """
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{env_key}="
    replacement = f"{env_key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


@router.post("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderCredentialUpdate, request: Request) -> dict[str, Any]:
    definition = _provider_definition(provider_id)
    if not definition.env_key:
        raise HTTPException(status_code=400, detail="This provider uses browser authentication; use the CLI login flow")
    home = current_services().require("north_home")
    await asyncio.to_thread(_write_env_key, home, definition.env_key, body.api_key.strip())
    os.environ[definition.env_key] = body.api_key.strip()
    await _refresh_inference_runtime(request.app)
    return {"id": definition.id, "configured": True, "credential_hint": body.api_key[:4] + "…" + body.api_key[-4:]}


def _allowed_output_roots() -> list[Path]:
    """Every directory the artifact library may read from.

    ``tasks/`` is where the engineering pipeline writes what it actually
    concluded - research notes, specs, implementation notes, QA reports. It was
    not readable from anywhere, which is why the researcher was told to copy its
    findings into the user's repo as well: the real artifact had nowhere visible
    to live. Listing it here is what makes that duplicate unnecessary.
    """
    home = current_services().require("north_home")
    return [home / name for name in ("news", "notes", "wellness", "tasks")]


def _is_readable_artifact(path: Path) -> bool:
    """False for north's own state files, which share ~/.north/tasks with the
    handoff directories. ``tasks.db`` and its WAL/SHM siblings sit right beside
    them, and adding that root to the library would otherwise publish the
    task-state database over HTTP. Same suffix list the tool sandbox blocks on.
    """
    return path.is_file() and not path.name.endswith(DB_SUFFIXES)


def _artifact_task_id(path: Path, home: Path) -> str:
    """The task a handoff artifact belongs to, or "" for a personal output."""
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        return ""
    return parts[1] if len(parts) > 2 and parts[0] == "tasks" else ""


def _artifact_id(path: Path) -> str:
    home = current_services().require("north_home")
    relative = str(path.relative_to(home))
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


def _artifact_path(artifact_id: str) -> Path:
    try:
        padded = artifact_id + "=" * (-len(artifact_id) % 4)
        relative = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    home = current_services().require("north_home").resolve()
    path = (home / relative).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in _allowed_output_roots()):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not _is_readable_artifact(path):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return path


def _list_artifacts_sync(limit: int) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for root in _allowed_output_roots():
        if root.exists():
            paths.extend(path for path in root.rglob("*") if _is_readable_artifact(path))
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    home = current_services().require("north_home")
    result = []
    for path in paths[: max(1, min(limit, 500))]:
        stat = path.stat()
        result.append(
            {
                "id": _artifact_id(path),
                "name": path.name,
                "kind": path.parent.name,
                "task": _artifact_task_id(path, home),
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
        "task": _artifact_task_id(path, current_services().require("north_home")),
        "media_type": mimetypes.guess_type(path.name)[0] or "text/plain",
        "content": content,
    }


@router.get("/dashboard")
async def dashboard() -> dict[str, Any]:
    active, jobs, cron, metrics, ledger_entries, conversations, artifacts = await asyncio.gather(
        current_services().require("orchestrator").list_active_tasks(),
        current_services().require("job_processor").list_jobs(limit=8),
        current_services().require("cron_store").list(),
        current_services().require("ledger").get_metrics(days=7),
        current_services().require("ledger").query(LedgerFilters(limit=12)),
        current_services().require("conversation_store").list(limit=6),
        list_artifacts(limit=6),
    )
    agents = current_services().require("agent_registry").all()
    settings = current_services().require("north_settings")
    return {
        "system": {"status": "online", "power": settings.power.value, "autonomy": settings.autonomy.value},
        "attention": [card.model_dump(mode="json") for card in current_services().require("approval_store").pending()],
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
