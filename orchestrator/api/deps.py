"""Shared wiring for the orchestrator's HTTP routes.

Every route module imports its accessors from here, so the routers stay pure
request handling and the wiring is described in one place. The components
themselves live on `app.state` - see `orchestrator/api_context.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from agents.registry import AgentRegistry
from config.strategy import NorthSettings
from inference.base import InferenceRouter
from jobs.base import JobProcessor
from jobs.cron_store import UserCronStore
from ledger.base import LedgerWriter
from memory.base import ContextStore
from memory.injection import ContextInjector
from orchestrator.agent_runs import AgentRunStore
from orchestrator.api_context import bind_request_services, current_services, merge
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream import EventStreamManager
from tools.confidence import ConfidenceTracker
from utils.security import verify_api_access

router = APIRouter(
    prefix="/orchestrator",
    tags=["orchestrator"],
    # bind_request_services first so route bodies (and the helpers they call)
    # can reach this app's wiring through current_services().
    dependencies=[Depends(bind_request_services), Depends(verify_api_access)],
)

# Unauthenticated router - only hosts /health so Docker / load-balancer probes
# don't need the API secret.
health_router = APIRouter(tags=["health"])


def configure(
    app: FastAPI,
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
    agent_run_store: AgentRunStore | None = None,
) -> None:
    """Attach this app's wiring. Called once in the lifespan.

    The components live on ``app.state`` rather than in module globals, so two
    apps can coexist and a test can wire its own without mutating import-time
    state (CODING_STYLE §22).
    """
    merge(
        app,
        orchestrator=orchestrator,
        stream_manager=stream_manager,
        ledger=ledger,
        agent_registry=agent_registry,
        context_store=context_store,
        context_injector=context_injector,
        job_processor=job_processor,
        inference_router=inference_router,
        confidence_tracker=confidence_tracker,
        cron_store=cron_store,
        north_settings=north_settings,
        agent_run_store=agent_run_store,
    )


def _get_orchestrator() -> Orchestrator:
    return current_services().require("orchestrator")


def _get_stream_manager() -> EventStreamManager:
    return current_services().require("stream_manager")


def _get_ledger() -> LedgerWriter:
    return current_services().require("ledger")


def _get_agent_run_store() -> AgentRunStore:
    return current_services().require("agent_run_store")


def _get_agent_registry() -> AgentRegistry:
    return current_services().require("agent_registry")


def _get_context_store() -> ContextStore:
    return current_services().require("context_store")


def _get_context_injector() -> ContextInjector:
    return current_services().require("context_injector")


def _get_job_processor() -> JobProcessor:
    return current_services().require("job_processor")


def _get_inference_router() -> InferenceRouter:
    return current_services().require("inference_router")


def _get_confidence_tracker() -> ConfidenceTracker:
    return current_services().require("confidence_tracker")


def _get_cron_store() -> UserCronStore:
    return current_services().require("cron_store")
