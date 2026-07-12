"""Dependency injection wire-up.

All components that can be constructed synchronously and do not have
circular dependencies are built here.  The remaining pieces  -
``AgentRegistry``, ``Orchestrator``, and friends - are assembled in
``orchestrator/app.py`` because they either require async initialisation
or have circular construction order (agent_registry ↔ agent_deps).

See docs/CODING_STYLE.md Section 6.3.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from approval import Notifier, TerminalNotifier
from approval.store import ApprovalStore
from config.settings import settings
from config.strategy import NorthSettings
from inference import InferenceRouter
from inference.factory import build_router
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter
from memory import ContextStore, FileContextStore

if TYPE_CHECKING:
    from context.code_index import CodeIndex
    from inference.cost_tracker import CostTracker
    from jobs.cron_store import UserCronStore
    from memory import MemoryGateway
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore
    from orchestrator.plan_store import PlanStore
    from orchestrator.running_tasks import RunningTaskStore
    from orchestrator.stream import EventStreamManager
    from orchestrator.task_context import TaskContextStore
    from tools.confidence import ConfidenceTracker

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

logger = logging.getLogger(__name__)


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
    running_task_store: RunningTaskStore
    plan_store: PlanStore
    north_settings: NorthSettings
    memory: MemoryGateway
    # Shared async callable used by EpisodicStore, EmbeddingIndex, ToolIndex,
    # and FactStore - guarantees a single embedding model and billing surface.
    embed_fn: EmbedFn | None = field(default=None)
    fact_store: FactStore | None = field(default=None)
    # Semantic code index (#2 code RAG). Present only when embeddings are available;
    # backs the search_code tool. None when no embed_fn is wired.
    code_index: CodeIndex | None = field(default=None)


def _resolve_preferred_models() -> dict[str, list[str]]:
    """Resolve the startup preferred-models default: env override, else built-in.

    ``NORTH_PREFERRED_MODELS`` (a JSON object string) overrides the curated
    ``DEFAULT_PREFERRED_MODELS``. A malformed value falls back to the default so
    a bad env var can never break startup. settings.json overrides this at runtime.
    """
    import json

    from inference.model_policy import DEFAULT_PREFERRED_MODELS, parse_preferred

    raw = settings.preferred_models.strip()
    if not raw:
        return {k: list(v) for k, v in DEFAULT_PREFERRED_MODELS.items()}
    try:
        parsed = parse_preferred(json.loads(raw))
    except Exception:
        logger.warning("NORTH_PREFERRED_MODELS is not valid JSON - using built-in defaults")
        return {k: list(v) for k, v in DEFAULT_PREFERRED_MODELS.items()}
    return parsed or {k: list(v) for k, v in DEFAULT_PREFERRED_MODELS.items()}


def build_production_dependencies(north_settings: NorthSettings | None = None) -> Dependencies:
    """Build and wire all synchronously-constructable production dependencies."""
    from context.code_index import CodeIndex
    from inference.cost_tracker import CostTracker
    from inference.models import EmbedRequest
    from jobs.cron_store import UserCronStore
    from memory import LocalMemoryGateway
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore
    from orchestrator.plan_store import PlanStore
    from orchestrator.running_tasks import RunningTaskStore
    from orchestrator.stream import EventStreamManager
    from orchestrator.task_context import TaskContextStore
    from tools.confidence import ConfidenceTracker

    if north_settings is None:
        from approval.mode import resolve_approval_mode

        north_settings = NorthSettings(
            settings.north_home / "settings.json",
            default_approval_mode=resolve_approval_mode(settings),
            default_preferred_models=_resolve_preferred_models(),
        )

    context_dir = settings.north_home / "context"
    legacy_public = context_dir / "public.md"
    user_doc = context_dir / "user.md"
    if legacy_public.exists() and not user_doc.exists():
        # 2b memory model: public.md was renamed to user.md. Preserve the user's
        # existing facts document under the new name (idempotent, one-time).
        legacy_public.rename(user_doc)
    context_store = FileContextStore(context_dir)
    ledger = SQLiteLedgerWriter(settings.north_home / "ledger.db")
    confidence_tracker = ConfidenceTracker(db_path=settings.north_home / "tools.db")
    base_router = build_router(
        openrouter_api_key=settings.openrouter_api_key,
        north_settings=north_settings,
        groq_api_key=settings.groq_api_key,
        gemini_api_key=settings.gemini_api_key,
        confidence_tracker=confidence_tracker,
        cooldowns_path=settings.north_home / "cooldowns.json",
    )
    cost_tracker = CostTracker(base_router)

    async def _embed_fn(texts: list[str]) -> list[list[float]]:
        resp = await cost_tracker.embed(EmbedRequest(texts=texts, component="embed"))
        return resp.embeddings

    episodic_store = EpisodicStore(db_path=settings.north_home / "episodic.db", embed_fn=_embed_fn)
    fact_store = FactStore(db_path=settings.north_home / "facts.db", embed_fn=_embed_fn)
    code_index = CodeIndex(db_path=settings.north_home / "code_index.db", embed_fn=_embed_fn)
    memory = LocalMemoryGateway(
        context_store=context_store,
        fact_store=fact_store,
        episodic_store=episodic_store,
    )

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
        episodic_store=episodic_store,
        task_context_store=TaskContextStore(),
        running_task_store=RunningTaskStore(settings.north_home / "running_tasks.db"),
        plan_store=PlanStore(),
        north_settings=north_settings,
        memory=memory,
        embed_fn=_embed_fn,
        fact_store=fact_store,
        code_index=code_index,
    )
