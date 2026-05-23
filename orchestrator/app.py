"""FastAPI application entry point for the Orchestrator server (port 8000).

See docs/CODING_STYLE.md Sections 10.4, 12, 17.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agents.models import AgentDependencies
from agents.registry import AgentRegistry
from config.dependencies import build_production_dependencies
from config.settings import settings
from context.extraction import ExtractionPipeline
from context.injection import ContextInjector
from jobs.models import Job
from jobs.scheduler import CronScheduler, V1_CRON_ENTRIES
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.api_router import configure as configure_api
from orchestrator.api_router import router as orchestrator_router
from orchestrator.classifier import IntentClassifier
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import TaskRequest
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.task_context import TaskContextStore
from tools.confidence import ConfidenceTracker
from tools.registry import TOOL_GRAPH, ToolRegistry
from utils.ids import generate_id
from utils.security import load_secret
from utils.time import utcnow
from approval.callback_server import app as callback_app
from approval.judgement_filter import JudgementFilter
from web.routes import configure as configure_web
from web.routes import router as web_router

logger = logging.getLogger(__name__)


async def _cleanup_stale_task_files(north_home: Path) -> int:
    """Delete task SQLite files older than 7 days. Returns the count removed."""
    tasks_dir = north_home / "tasks"
    if not tasks_dir.exists():
        return 0

    cutoff = utcnow() - datetime.timedelta(days=7)
    deleted = 0

    def _run() -> int:
        count = 0
        for db_file in tasks_dir.glob("task_*.db"):
            mtime = datetime.datetime.fromtimestamp(
                db_file.stat().st_mtime, tz=datetime.timezone.utc
            )
            if mtime < cutoff:
                db_file.unlink(missing_ok=True)
                count += 1
        return count

    return await asyncio.to_thread(_run)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Build production dependencies and launch background tasks."""
    import sys

    def _step(msg: str) -> None:
        print(f"  [startup] {msg}", flush=True, file=sys.stderr)

    _step("loading secret")
    load_secret()

    _step("building dependencies")
    deps = build_production_dependencies()

    _step("building tool registry")
    tool_registry = ToolRegistry(graph=TOOL_GRAPH, auto_register=True)
    _step("building confidence tracker")
    confidence_tracker = ConfidenceTracker(db_path=settings.north_home / "tools.db")

    agent_deps = AgentDependencies(
        context_store=deps.context_store,
        inference_router=deps.inference_router,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
    )
    _step("scanning agent registry")
    agents_dir = Path(__file__).parent.parent / "agents"
    agent_registry = AgentRegistry(agents_dir=agents_dir, deps=agent_deps)
    _step(f"registered agents: {agent_registry.names()}")

    stream_manager = EventStreamManager()
    task_context_store = TaskContextStore()
    failure_handler = FailureHandler(
        ledger_writer=deps.ledger,
        task_context_store=task_context_store,
        stream_manager=stream_manager,
    )

    judgement_filter = JudgementFilter(
        context_store=deps.context_store,
        inference_router=deps.inference_router,
    )

    orchestrator = Orchestrator(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        classifier=IntentClassifier(inference_router=deps.inference_router),
        north_star_checker=NorthStarChecker(
            context_store=deps.context_store,
            inference_router=deps.inference_router,
        ),
        execution_planner=ExecutionPlanner(
            agent_registry=agent_registry,
            inference_router=deps.inference_router,
        ),
        task_context_store=task_context_store,
        failure_handler=failure_handler,
        notifier=deps.notifier,
        stream_manager=stream_manager,
        judgement_filter=judgement_filter,
    )

    context_injector = ContextInjector(
        context_store=deps.context_store,
        inference_router=deps.inference_router,
        ledger=deps.ledger,
    )

    extraction_pipeline = ExtractionPipeline(
        ledger=deps.ledger,
        context_store=deps.context_store,
        inference_router=deps.inference_router,
        north_home=settings.north_home,
    )

    # ── Job dispatcher: maps cron/async jobs to Orchestrator tasks ──────────

    async def _dispatch_job(job: Job) -> None:
        if job.task == "task_context_cleanup":
            n = await _cleanup_stale_task_files(settings.north_home)
            asyncio.create_task(deps.ledger.write(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                action=f"task_context_cleanup: removed {n} stale task files",
                status=LedgerStatus.COMPLETED,
            )))
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
    )

    # ── Background tasks ─────────────────────────────────────────────────────

    _step("building cron scheduler")
    cron_scheduler = CronScheduler(
        processor=deps.job_processor,
        entries=V1_CRON_ENTRIES,
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


app = FastAPI(
    title="north Orchestrator",
    description="Personal Life Operating System — core API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(orchestrator_router)
app.include_router(web_router)

_static_dir = Path(__file__).parent.parent / "web" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
