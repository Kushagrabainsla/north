"""FastAPI application entry point for the Orchestrator server (port 8000).

See docs/CODING_STYLE.md Sections 10.4, 12, 17.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agents.models import AgentDependencies
from agents.registry import AgentRegistry
from approval.callback_server import app as callback_app
from approval.judgement_filter import JudgementFilter
from approval.tui import TUIAwareNotifier
from config.dependencies import build_production_dependencies
from config.settings import settings
from context.embedding_index import EmbeddingIndex
from context.extraction import ExtractionPipeline
from context.file_store import FileContextStore
from context.injection import ContextInjector
from jobs.models import Job
from jobs.scheduler import V1_CRON_ENTRIES, CronScheduler
from ledger.base import LedgerFilters
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.api_router import configure as configure_api
from orchestrator.api_router import health_router, webhook_router
from orchestrator.api_router import router as orchestrator_router
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import TaskRequest
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.router import ExecutionPlanner
from orchestrator.synthesizer import ResultSynthesizer
from tools.registry import ToolRegistry
from tools.specialized.bash import BashTool
from tools.universal.create_tool import CreateToolTool
from tools.universal.schedule_task import ScheduleTaskTool
from utils.ids import generate_id
from utils.logging import configure_structured_logging
from utils.security import load_secret
from utils.time import utcnow
from web.routes import configure as configure_web
from web.routes import router as web_router

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).parent.parent / "agents"

_RELIABLE_TOOLS = frozenset({
    "read_file", "write_file", "list_dir", "search_files", "bash",
    "web_search", "schedule_task", "fetch_url", "git", "patch_file",
})


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def _step(msg: str) -> None:
    log_file = os.environ.get("NORTH_LOG_FILE", "").strip()
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"  [startup] {msg}\n")
    else:
        print(f"  [startup] {msg}", flush=True, file=sys.stderr)


def _validate_config() -> None:
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "NORTH_OPENROUTER_API_KEY is not set. "
            "Get a key at https://openrouter.ai/keys and add it to your .env file."
        )


def _attach_tui_notifier(deps) -> None:
    # Suppress macOS/terminal alerts while the TUI is connected — the global
    # SSE stream handles approvals inline.
    deps.notifier = TUIAwareNotifier(
        stream_manager=deps.stream_manager,
        fallback=deps.notifier,
    )


def _attach_embedding_index(deps) -> None:
    embedding_index = EmbeddingIndex(
        db_path=settings.north_home / "embeddings.db",
        embed_fn=deps.embed_fn,
    )
    # Direct assignment because FileContextStore exposes no setter — write/append
    # calls trigger re-indexing once the index is attached.
    if isinstance(deps.context_store, FileContextStore):
        deps.context_store._embedding_index = embedding_index


def _build_tool_registry(deps, tool_graph) -> ToolRegistry:
    tool_registry = ToolRegistry(graph=tool_graph, auto_register=True)
    tool_registry.register(
        ScheduleTaskTool(job_processor=deps.job_processor, cron_store=deps.cron_store)
    )
    tool_registry.make_universal("schedule_task")
    tool_registry.register(CreateToolTool(tool_registry=tool_registry))
    # BashTool gates every command behind user approval and cannot be auto-discovered.
    tool_registry.register(
        BashTool(
            approval_store=deps.approval_store,
            stream_manager=deps.stream_manager,
            approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
        )
    )
    return tool_registry


def _build_agent_deps(deps, tool_registry: ToolRegistry) -> AgentDependencies:
    return AgentDependencies(
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        tool_registry=tool_registry,
        confidence_tracker=deps.confidence_tracker,
        stream_manager=deps.stream_manager,
        episodic_store=deps.episodic_store,
        approval_store=deps.approval_store,
        agent_max_iterations=settings.agent_max_iterations,
        agent_history_keep_recent=settings.agent_history_keep_recent,
        approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
    )


def _build_agent_registry(agent_deps: AgentDependencies) -> AgentRegistry:
    registry = AgentRegistry(agents_dir=_AGENTS_DIR, deps=agent_deps)
    # Break the circular dependency: agents need the registry to delegate sub-tasks,
    # but the registry needs agent_deps to instantiate agents.
    agent_deps.agent_registry = registry
    return registry


def _build_extraction_pipeline(deps) -> ExtractionPipeline:
    return ExtractionPipeline(
        ledger=deps.ledger,
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        north_home=settings.north_home,
    )


def _build_orchestrator(
    deps,
    agent_registry: AgentRegistry,
    tool_registry: ToolRegistry,
    extraction_pipeline: ExtractionPipeline,
) -> Orchestrator:
    return Orchestrator(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        north_star_checker=NorthStarChecker(
            context_store=deps.context_store,
            inference_router=deps.cost_tracker,
        ),
        execution_planner=ExecutionPlanner(
            agent_registry=agent_registry,
            inference_router=deps.cost_tracker,
            tool_registry=tool_registry,
        ),
        task_context_store=deps.task_context_store,
        failure_handler=FailureHandler(
            ledger_writer=deps.ledger,
            task_context_store=deps.task_context_store,
            stream_manager=deps.stream_manager,
        ),
        notifier=deps.notifier,
        stream_manager=deps.stream_manager,
        approval_store=deps.approval_store,
        judgement_filter=JudgementFilter(
            context_store=deps.context_store,
            inference_router=deps.cost_tracker,
        ),
        north_settings=deps.north_settings,
        synthesizer=ResultSynthesizer(inference_router=deps.cost_tracker),
        tracked_router=deps.cost_tracker,
        episodic_store=deps.episodic_store,
        tool_registry=tool_registry,
        default_workspace=settings.north_workspace,
        extraction_pipeline=extraction_pipeline,
    )


def _build_context_injector(deps) -> ContextInjector:
    return ContextInjector(
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        ledger=deps.ledger,
    )


async def _reconcile_pending_tasks(deps, orchestrator: Orchestrator) -> None:
    try:
        pending_entries = await deps.ledger.query(
            LedgerFilters(status=LedgerStatus.PENDING, limit=500)
        )
        pending_task_ids = {e.task_id for e in pending_entries if e.task_id}
        orphaned = pending_task_ids - set(orchestrator._active_tasks)
        for orphaned_id in orphaned:
            await deps.ledger.write(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=orphaned_id,
                action="task_failed",
                output="Server restarted while task was pending — marked as failed.",
                status=LedgerStatus.FAILED,
            ))
        if orphaned:
            logger.warning(
                "Reconciliation: marked %d orphaned PENDING task(s) as FAILED: %s",
                len(orphaned), list(orphaned),
            )
        else:
            logger.info("Reconciliation: no orphaned PENDING tasks found.")
    except Exception:
        logger.exception("Startup reconciliation sweep failed")


def _configure_routers(orchestrator, deps, agent_registry, context_injector) -> None:
    configure_api(
        orchestrator=orchestrator,
        stream_manager=deps.stream_manager,
        ledger=deps.ledger,
        agent_registry=agent_registry,
        context_store=deps.context_store,
        context_injector=context_injector,
        job_processor=deps.job_processor,
        inference_router=deps.inference_router,
        confidence_tracker=deps.confidence_tracker,
        cron_store=deps.cron_store,
        north_settings=deps.north_settings,
    )
    configure_web(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        context_store=deps.context_store,
        context_injector=context_injector,
        job_processor=deps.job_processor,
        inference_router=deps.inference_router,
        confidence_tracker=deps.confidence_tracker,
        cron_store=deps.cron_store,
        approval_store=deps.approval_store,
    )


def _build_callback_server() -> uvicorn.Server:
    config = uvicorn.Config(callback_app, host="127.0.0.1", port=8001, log_level="warning")
    server = uvicorn.Server(config)
    # Prevents the nested uvicorn from overriding the outer server's SIGTERM/SIGINT handlers on macOS.
    server.install_signal_handlers = lambda: None
    return server


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _guarded(coro, name: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("background task %r failed", name)


async def _pool_refresh_loop(deps) -> None:
    interval = settings.inference_pool_refresh_interval_hours * 3600
    while True:
        try:
            await deps.inference_router.refresh_pools()
            logger.info("Inference pool refreshed successfully")
        except Exception:
            logger.warning("Inference pool refresh failed", exc_info=True)
        await asyncio.sleep(interval)


def _launch_background_tasks(
    deps, orchestrator: Orchestrator, extraction_pipeline: ExtractionPipeline, callback_server: uvicorn.Server
) -> list[asyncio.Task]:
    async def _dispatch_job(job: Job) -> None:
        if job.task == "task_context_cleanup":
            n = await deps.task_context_store.cleanup_stale_tasks(
                active_task_ids=frozenset(orchestrator._active_tasks),
            )
            await deps.ledger.write(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                action=f"task_context_cleanup: removed {n} stale rows",
                status=LedgerStatus.COMPLETED,
            ))
            return
        await orchestrator.submit_task(
            TaskRequest(prompt=f"[scheduled] {job.task}", source=LedgerSource.CRON)
        )

    cron_scheduler = CronScheduler(
        processor=deps.job_processor,
        entries=V1_CRON_ENTRIES,
        cron_store=deps.cron_store,
    )

    return [
        asyncio.create_task(
            _guarded(
                deps.job_processor.run(
                    on_job=_dispatch_job,
                    poll_interval_seconds=settings.job_poll_interval_seconds,
                ),
                "job_processor",
            ),
            name="job_processor",
        ),
        asyncio.create_task(_guarded(cron_scheduler.run(), "cron_scheduler"), name="cron_scheduler"),
        asyncio.create_task(_guarded(extraction_pipeline.run(), "extraction_pipeline"), name="extraction_pipeline"),
        asyncio.create_task(_guarded(callback_server.serve(), "callback_server"), name="callback_server"),
        asyncio.create_task(_guarded(_pool_refresh_loop(deps), "pool_refresh"), name="pool_refresh"),
    ]


async def _shutdown(deps, callback_server: uvicorn.Server, background_tasks: list[asyncio.Task]) -> None:
    callback_server.should_exit = True
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await deps.cost_tracker.aclose()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_structured_logging()

    _step("loading secret")
    load_secret()
    _validate_config()

    _step("building dependencies")
    deps = build_production_dependencies()
    _attach_tui_notifier(deps)

    _step("building embedding index")
    _attach_embedding_index(deps)

    _step("building tool registry")
    tool_graph = AgentRegistry.build_tool_graph(_AGENTS_DIR)
    tool_registry = _build_tool_registry(deps, tool_graph)

    _step("seeding confidence defaults")
    await deps.confidence_tracker.seed_defaults(tool_graph, _RELIABLE_TOOLS)

    _step("scanning agent registry")
    agent_deps = _build_agent_deps(deps, tool_registry)
    agent_registry = _build_agent_registry(agent_deps)
    _step(f"registered agents: {agent_registry.names()}")

    extraction_pipeline = _build_extraction_pipeline(deps)
    orchestrator = _build_orchestrator(deps, agent_registry, tool_registry, extraction_pipeline)
    context_injector = _build_context_injector(deps)

    _step("running startup reconciliation sweep")
    await _reconcile_pending_tasks(deps, orchestrator)

    _step("configuring API router")
    _step("configuring web router")
    _configure_routers(orchestrator, deps, agent_registry, context_injector)

    _step("configuring callback server")
    callback_server = _build_callback_server()

    _step("scheduling background tasks")
    background_tasks = _launch_background_tasks(deps, orchestrator, extraction_pipeline, callback_server)

    _step("startup complete — yielding to server")
    try:
        yield
    finally:
        await _shutdown(deps, callback_server, background_tasks)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="north Orchestrator",
    description="Personal Life Operating System — core API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(orchestrator_router)
app.include_router(webhook_router)
app.include_router(web_router)

_static_dir = Path(__file__).parent.parent / "web" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
