"""Dependency injection wire-up.

All components that can be constructed synchronously and do not have
circular dependencies are built here.  The remaining pieces —
``AgentRegistry``, ``Orchestrator``, and friends — are assembled in
``orchestrator/app.py`` because they either require async initialisation
or have circular construction order (agent_registry ↔ agent_deps).

See docs/CODING_STYLE.md Section 6.3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from approval import Notifier, TerminalNotifier
from approval.store import ApprovalStore
from config.settings import settings
from config.strategy import NorthSettings
from context import ContextStore, FileContextStore
from inference import InferenceRouter
from inference.factory import build_router
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter

if TYPE_CHECKING:
    from context.episodic import EpisodicStore
    from context.fact_store import FactStore
    from inference.cost_tracker import CostTracker
    from jobs.cron_store import UserCronStore
    from orchestrator.stream import EventStreamManager
    from orchestrator.task_context import TaskContextStore
    from tools.confidence import ConfidenceTracker

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass
class Dependencies:
    """Full dependency container built once at startup.

    Covers every component that does not require async initialisation or
    has a circular construction dependency.  ``app.py`` reads from this
    object instead of constructing components inline.
    """

    context_store: ContextStore
    ledger: LedgerWriter
    inference_router: InferenceRouter
    notifier: Notifier
    job_processor: JobProcessor
    cost_tracker: CostTracker
    stream_manager: EventStreamManager
    approval_store: ApprovalStore
    cron_store: UserCronStore
    confidence_tracker: ConfidenceTracker
    episodic_store: EpisodicStore
    task_context_store: TaskContextStore
    north_settings: NorthSettings
    # Shared async callable used by EpisodicStore, EmbeddingIndex, ToolIndex,
    # and FactStore — guarantees a single embedding model and billing surface.
    embed_fn: EmbedFn | None = field(default=None)
    fact_store: FactStore | None = field(default=None)


def build_production_dependencies(north_settings: NorthSettings | None = None) -> Dependencies:
    """Build and wire all synchronously-constructable production dependencies."""
    from context.episodic import EpisodicStore
    from context.fact_store import FactStore
    from inference.cost_tracker import CostTracker
    from inference.models import EmbedRequest
    from jobs.cron_store import UserCronStore
    from orchestrator.stream import EventStreamManager
    from orchestrator.task_context import TaskContextStore
    from tools.confidence import ConfidenceTracker

    if north_settings is None:
        north_settings = NorthSettings(settings.north_home / "settings.json")

    context_store = FileContextStore(settings.north_home / "context")
    ledger = SQLiteLedgerWriter(settings.north_home / "ledger.db")
    confidence_tracker = ConfidenceTracker(db_path=settings.north_home / "tools.db")
    base_router = build_router(
        openrouter_api_key=settings.openrouter_api_key,
        north_settings=north_settings,
        groq_api_key=settings.groq_api_key,
        gemini_api_key=settings.gemini_api_key,
        confidence_tracker=confidence_tracker,
    )
    cost_tracker = CostTracker(base_router)

    async def _embed_fn(texts: list[str]) -> list[list[float]]:
        resp = await cost_tracker.embed(EmbedRequest(texts=texts, component="embed"))
        return resp.embeddings

    return Dependencies(
        context_store=context_store,
        ledger=ledger,
        inference_router=base_router,
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(settings.north_home / "jobs.db"),
        cost_tracker=cost_tracker,
        stream_manager=EventStreamManager(),
        approval_store=ApprovalStore(),
        cron_store=UserCronStore(settings.north_home / "jobs.db"),
        confidence_tracker=confidence_tracker,
        episodic_store=EpisodicStore(
            db_path=settings.north_home / "episodic.db",
            embed_fn=_embed_fn,
        ),
        task_context_store=TaskContextStore(),
        north_settings=north_settings,
        embed_fn=_embed_fn,
        fact_store=FactStore(
            db_path=settings.north_home / "facts.db",
            embed_fn=_embed_fn,
        ),
    )
