"""FastAPI application entry point for the Orchestrator server (port 8000).

See docs/CODING_STYLE.md Sections 10.4, 12, 17.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agents.models import AgentDependencies
from config.strategy import NorthSettings
from agents.registry import AgentRegistry
from config.dependencies import build_production_dependencies
from config.settings import settings
from context.embedding_index import EmbeddingIndex
from context.episodic import EpisodicStore
from context.extraction import ExtractionPipeline
from context.injection import ContextInjector
from jobs.cron_store import UserCronStore
from jobs.models import Job
from jobs.scheduler import CronScheduler, V1_CRON_ENTRIES
from tools.implementations.schedule_task import ScheduleTaskTool
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.api_router import configure as configure_api
from orchestrator.api_router import router as orchestrator_router
from orchestrator.api_router import webhook_router
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import TaskRequest
from inference.cost_tracker import CostTracker
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.task_context import TaskContextStore
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry
from utils.ids import generate_id
from utils.security import load_secret
from utils.time import utcnow
from approval.callback_server import app as callback_app
from approval.judgement_filter import JudgementFilter
from approval.store import ApprovalStore
from web.routes import configure as configure_web
from web.routes import router as web_router

logger = logging.getLogger(__name__)




@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Build production dependencies and launch background tasks."""
    import sys

    def _step(msg: str) -> None:
        print(f"  [startup] {msg}", flush=True, file=sys.stderr)

    _step("loading secret")
    load_secret()

    if not settings.openrouter_api_key:
        raise RuntimeError(
            "NORTH_OPENROUTER_API_KEY is not set. "
            "Get a key at https://openrouter.ai/keys and add it to your .env file."
        )

    _step("loading north settings")
    north_settings = NorthSettings(settings.north_home / "settings.json")

    _step("building dependencies")
    deps = build_production_dependencies(north_settings=north_settings)
    cost_tracker = CostTracker(deps.inference_router)

    async def _embed_fn(texts: list[str]) -> list[list[float]]:
        from inference.models import EmbedRequest
        resp = await cost_tracker.embed(EmbedRequest(texts=texts, component="embed"))
        return resp.embeddings

    _step("building embedding index")
    embedding_index = EmbeddingIndex(
        db_path=settings.north_home / "embeddings.db",
        embed_fn=_embed_fn,
    )

    _step("building episodic store")
    episodic_store = EpisodicStore(
        db_path=settings.north_home / "episodic.db",
        embed_fn=_embed_fn,
    )

    # Attach embedding index to the context store so write/append trigger re-indexing.
    from context.file_store import FileContextStore
    if isinstance(deps.context_store, FileContextStore):
        deps.context_store._embedding_index = embedding_index

    _step("building cron store")
    cron_store = UserCronStore(settings.north_home / "jobs.db")

    _step("building tool registry")
    agents_dir = Path(__file__).parent.parent / "agents"
    dynamic_tool_graph = AgentRegistry.build_tool_graph(agents_dir)
    tool_registry = ToolRegistry(graph=dynamic_tool_graph, auto_register=True)
    tool_registry.register(ScheduleTaskTool(job_processor=deps.job_processor, cron_store=cron_store))
    _step("building confidence tracker")
    confidence_tracker = ConfidenceTracker(db_path=settings.north_home / "tools.db")
    _RELIABLE_TOOLS = frozenset({
        "read_file", "write_file", "list_dir", "search_files", "bash",
        "web_search", "schedule_task", "fetch_url", "git", "patch_file",
    })
    await confidence_tracker.seed_defaults(dynamic_tool_graph, _RELIABLE_TOOLS)

    stream_manager = EventStreamManager()

    # Single ApprovalStore instance shared by Orchestrator and every agent so
    # all approval waits and resolutions touch the same in-memory registry.
    approval_store = ApprovalStore()

    agent_deps = AgentDependencies(
        context_store=deps.context_store,
        inference_router=cost_tracker,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
        stream_manager=stream_manager,
        episodic_store=episodic_store,
        approval_store=approval_store,
    )
    _step("scanning agent registry")
    agent_registry = AgentRegistry(agents_dir=agents_dir, deps=agent_deps)
    # Wire agent_registry back into deps so agents can delegate sub-tasks.
    agent_deps.agent_registry = agent_registry
    _step(f"registered agents: {agent_registry.names()}")
    task_context_store = TaskContextStore()
    failure_handler = FailureHandler(
        ledger_writer=deps.ledger,
        task_context_store=task_context_store,
        stream_manager=stream_manager,
    )

    judgement_filter = JudgementFilter(
        context_store=deps.context_store,
        inference_router=cost_tracker,
    )

    orchestrator = Orchestrator(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        north_star_checker=NorthStarChecker(
            context_store=deps.context_store,
            inference_router=cost_tracker,
        ),
        execution_planner=ExecutionPlanner(
            agent_registry=agent_registry,
            inference_router=cost_tracker,
            tool_registry=tool_registry,
        ),
        task_context_store=task_context_store,
        failure_handler=failure_handler,
        notifier=deps.notifier,
        stream_manager=stream_manager,
        judgement_filter=judgement_filter,
        north_settings=north_settings,
        synthesizer=ResultSynthesizer(inference_router=cost_tracker),
        cost_tracker=cost_tracker,
        episodic_store=episodic_store,
        tool_registry=tool_registry,
        approval_store=approval_store,
    )

    context_injector = ContextInjector(
        context_store=deps.context_store,
        inference_router=cost_tracker,
        ledger=deps.ledger,
    )

    extraction_pipeline = ExtractionPipeline(
        ledger=deps.ledger,
        context_store=deps.context_store,
        inference_router=cost_tracker,
        north_home=settings.north_home,
    )

    # ── Job dispatcher: maps cron/async jobs to Orchestrator tasks ──────────

    async def _dispatch_job(job: Job) -> None:
        if job.task == "task_context_cleanup":
            n = await task_context_store.cleanup_stale_tasks(
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

    # ── Wire API and Web routers ─────────────────────────────────────────────

    _step("configuring API router")
    configure_api(
        orchestrator=orchestrator,
        stream_manager=stream_manager,
        ledger=deps.ledger,
        agent_registry=agent_registry,
        context_store=deps.context_store,
        context_injector=context_injector,
        job_processor=deps.job_processor,
        inference_router=deps.inference_router,
        confidence_tracker=confidence_tracker,
        cron_store=cron_store,
        north_settings=north_settings,
    )

    _step("configuring web router")
    configure_web(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        context_store=deps.context_store,
        context_injector=context_injector,
        job_processor=deps.job_processor,
        inference_router=deps.inference_router,
        confidence_tracker=confidence_tracker,
        cron_store=cron_store,
    )

    # ── Background tasks ─────────────────────────────────────────────────────

    _step("building cron scheduler")
    cron_scheduler = CronScheduler(
        processor=deps.job_processor,
        entries=V1_CRON_ENTRIES,
        cron_store=cron_store,
    )

    # ── Callback server (port 8001) — receives macOS alerter decisions ──────
    # install_signal_handlers=False prevents the nested uvicorn from overriding
    # the outer server's SIGTERM/SIGINT handlers on macOS.

    _step("configuring callback server")
    callback_config = uvicorn.Config(
        callback_app,
        host="127.0.0.1",
        port=8001,
        log_level="warning",
    )
    callback_server = uvicorn.Server(callback_config)
    # Prevent nested uvicorn from overriding the outer server's signal handlers.
    callback_server.install_signal_handlers = lambda: None

    async def _guarded(coro, name: str):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background task %r failed", name)

    _step("scheduling background tasks")
    background_tasks = [
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
        asyncio.create_task(
            _guarded(cron_scheduler.run(), "cron_scheduler"), name="cron_scheduler"
        ),
        asyncio.create_task(
            _guarded(extraction_pipeline.run(), "extraction_pipeline"),
            name="extraction_pipeline",
        ),
        asyncio.create_task(
            _guarded(callback_server.serve(), "callback_server"), name="callback_server"
        ),
    ]

    _step("startup complete — yielding to server")
    try:
        yield
    finally:
        callback_server.should_exit = True
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await cost_tracker.aclose()


app = FastAPI(
    title="north Orchestrator",
    description="Personal Life Operating System — core API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(orchestrator_router)
app.include_router(webhook_router)
app.include_router(web_router)

_static_dir = Path(__file__).parent.parent / "web" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
