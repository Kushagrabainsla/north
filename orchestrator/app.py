"""FastAPI application entry point for the Orchestrator server (port 8000).

See docs/CODING_STYLE.md Sections 10.4, 12, 17.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agents.models import AgentDependencies
from agents.registry import AgentRegistry
from approval.card_factory import CardFactory
from config.dependencies import build_production_dependencies
from config.settings import settings
from orchestrator.api_router import configure as configure_router
from orchestrator.api_router import router as orchestrator_router
from orchestrator.classifier import IntentClassifier
from orchestrator.failure_handler import FailureHandler
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.task_context import TaskContextStore
from tools.confidence import ConfidenceTracker
from tools.registry import TOOL_GRAPH, TOOL_IMPLEMENTATIONS, ToolRegistry
from utils.security import load_secret
from web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Build production dependencies and launch background tasks."""
    # Ensure secret exists
    load_secret()

    deps = build_production_dependencies()

    # Build tool registry
    tool_registry = ToolRegistry(graph=TOOL_GRAPH, auto_register=True)

    # Build confidence tracker
    confidence_tracker = ConfidenceTracker(
        db_path=settings.north_home / "tools.db"
    )

    # Build agent registry
    agent_deps = AgentDependencies(
        context_store=deps.context_store,
        inference_router=deps.inference_router,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
    )
    agents_dir = Path(__file__).parent.parent / "agents"
    agent_registry = AgentRegistry(agents_dir=agents_dir, deps=agent_deps)

    # Build orchestrator components
    stream_manager = EventStreamManager()
    task_context_store = TaskContextStore()
    failure_handler = FailureHandler(
        ledger_writer=deps.ledger,
        task_context_store=task_context_store,
        stream_manager=stream_manager,
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
    )

    configure_router(orchestrator=orchestrator, stream_manager=stream_manager)

    # Start background job processor
    background_tasks = [
        asyncio.create_task(deps.job_processor.run(), name="job_processor"),
    ]

    try:
        yield
    finally:
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

# Mount static assets
_static_dir = Path(__file__).parent.parent / "web" / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
