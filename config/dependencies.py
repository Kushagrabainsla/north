"""Dependency injection wire-up.

All components that can be constructed synchronously and do not have
circular dependencies are built here.  The remaining pieces  -
``AgentRegistry``, ``Orchestrator``, and friends - are assembled in
``orchestrator/app.py`` because they either require async initialisation
or have circular construction order (agent_registry ↔ agent_deps).

See docs/CODING_STYLE.md Section 6.3.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from approval import Notifier, TerminalNotifier
from approval.store import ApprovalStore
from config.settings import settings
from config.strategy import NorthSettings
from inference import InferenceRouter
from inference.exceptions import EmbeddingCountMismatchError
from inference.factory import build_router
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter
from memory import ContextStore, SQLiteContextStore

if TYPE_CHECKING:
    from context.code_index import CodeIndex
    from inference.cost_tracker import CostTracker
    from jobs.cron_store import UserCronStore
    from memory import MemoryGateway
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore
    from orchestrator.agent_runs import AgentRunStore
    from orchestrator.plan_store import PlanStore
    from orchestrator.running_tasks import RunningTaskStore
    from orchestrator.stream import EventStreamManager
    from orchestrator.task_context import TaskContextStore
    from tools.confidence import ConfidenceTracker

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
# Given one new fact and the existing facts closest to it, return the indices of
# those the new fact makes untrue. Injected as a plain callable for the same
# reason as EmbedFn: the fact store owns storage, not inference.
SupersedeFn = Callable[[str, list[str]], Awaitable[list[int]]]

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
    agent_run_store: AgentRunStore
    north_settings: NorthSettings
    memory: MemoryGateway
    # Shared async callable used by EpisodicStore, EmbeddingIndex, ToolIndex,
    # and FactStore - guarantees a single embedding model and billing surface.
    embed_fn: EmbedFn | None = field(default=None)
    # The model behind embed_fn. Vector stores stamp themselves with it and drop
    # their contents when it changes, because vectors from two embedding models
    # cannot meaningfully be compared (see utils/vector_space.py).
    embedding_model: str = ""
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
    from orchestrator.agent_runs import AgentRunStore
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
    context_store = SQLiteContextStore(settings.north_home / "memory.db", legacy_path=context_dir)
    ledger = SQLiteLedgerWriter(settings.north_home / "ledger.db")
    confidence_tracker = ConfidenceTracker(db_path=settings.north_home / "tools.db")
    base_router = build_router(
        openrouter_api_key=settings.openrouter_api_key,
        north_settings=north_settings,
        groq_api_key=settings.groq_api_key,
        gemini_api_key=settings.gemini_api_key,
        opencode_zen_api_key=settings.opencode_zen_api_key,
        provider_settings=settings,
        confidence_tracker=confidence_tracker,
        cooldowns_path=settings.north_home / "cooldowns.json",
        models_db_path=settings.north_home / "models.db",
        routing_mode=settings.routing,
    )
    cost_tracker = CostTracker(base_router)

    _embed_cache: OrderedDict[str, list[float]] = OrderedDict()
    _EMBED_CACHE_MAX_SIZE = 512
    # Text → the in-flight request already embedding it. An agent run fans out
    # four concurrent recalls (facts, episodes, skills ×2) for the *same* prompt;
    # without this they all miss the cache and each pays a round trip.
    _embed_inflight: dict[str, asyncio.Future[list[float]]] = {}

    def _cache_put(text: str, emb: list[float]) -> None:
        _embed_cache[text] = emb
        _embed_cache.move_to_end(text)
        while len(_embed_cache) > _EMBED_CACHE_MAX_SIZE:
            _embed_cache.popitem(last=False)

    async def _embed_uncached(texts: list[str]) -> list[list[float]]:
        """Embed *texts*, returning exactly one vector per input, in order."""
        resp = await cost_tracker.embed(EmbedRequest(texts=texts, component="embed"))
        embeddings = list(resp.embeddings)
        if len(embeddings) != len(texts):
            # Callers zip these against their own lists (skills, tool descriptions,
            # code chunks). A short response silently shifts every later vector onto
            # the wrong item, so refuse it rather than corrupt the mapping.
            raise EmbeddingCountMismatchError(expected=len(texts), received=len(embeddings))
        return embeddings

    async def _embed_fn(texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        awaited: dict[str, asyncio.Future[list[float]]] = {}
        missing_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = _embed_cache.get(text)
            if cached is not None:
                _embed_cache.move_to_end(text)
                results[i] = cached
                continue
            inflight = _embed_inflight.get(text)
            if inflight is not None:
                awaited[text] = inflight
            elif text not in awaited:
                awaited[text] = asyncio.get_running_loop().create_future()
                _embed_inflight[text] = awaited[text]
                missing_texts.append(text)

        # Only the texts this call claimed are embedded; duplicates within the
        # batch and texts another coroutine is already fetching are awaited below.
        if missing_texts:
            try:
                embeddings = await _embed_uncached(missing_texts)
            except BaseException as exc:
                for text in missing_texts:
                    future = _embed_inflight.pop(text, None)
                    if future is not None and not future.done():
                        future.set_exception(exc)
                        # This caller re-raises rather than awaiting its own future,
                        # so consume the result here; otherwise asyncio logs a
                        # "Future exception was never retrieved" warning per text.
                        future.exception()
                raise
            for text, emb in zip(missing_texts, embeddings, strict=True):
                _cache_put(text, emb)
                future = _embed_inflight.pop(text, None)
                if future is not None and not future.done():
                    future.set_result(emb)

        for i, text in enumerate(texts):
            if results[i] is None:
                results[i] = await awaited[text]

        return [r for r in results if r is not None]

    # Which model produced a vector decides which vectors it can be compared
    # against, so every store is stamped with it and clears itself on a change.
    embedding_model = base_router.embedding_model_id()
    async def _supersede_fn(new_fact: str, candidates: list[str]) -> list[int]:
        """Which of *candidates* does *new_fact* make untrue? Returns their indices.

        Deliberately conservative: the model is told to answer only when the new
        fact genuinely replaces the old one, because wrongly retiring a fact
        loses information that nothing else will put back. "Related" and "about
        the same topic" are not enough - the old claim has to now be false.
        """
        from inference.models import CompletionRequest, PoolPriority
        from utils.text import extract_json

        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        prompt = (
            "A new fact about a person has just been recorded. Decide which of the "
            "existing facts it makes NO LONGER TRUE.\n\n"
            f"New fact:\n{new_fact}\n\nExisting facts:\n{numbered}\n\n"
            "Only list an existing fact if the new one genuinely replaces it - for example a "
            "role that has ended, a number that has changed, or a plan that has been carried "
            "out. Do NOT list a fact merely because it covers the same topic, adds detail, or "
            "sits alongside the new one. Two facts that can both be true must both stay.\n"
            'Reply with JSON only: {"superseded": [<indices>]}'
        )
        response = await cost_tracker.complete(
            CompletionRequest(
                prompt=prompt,
                priority=PoolPriority.LOW,
                component="fact_supersede",
                json_mode=True,
            )
        )
        result = extract_json(response.text.strip())
        if not isinstance(result, dict):
            return []
        raw = result.get("superseded") or []
        return [int(i) for i in raw if isinstance(i, int | str) and str(i).lstrip("-").isdigit()]

    episodic_store = EpisodicStore(
        db_path=settings.north_home / "episodic.db", embed_fn=_embed_fn, embedding_model=embedding_model
    )
    fact_store = FactStore(
        db_path=settings.north_home / "facts.db",
        embed_fn=_embed_fn,
        embedding_model=embedding_model,
        supersede_fn=_supersede_fn,
    )
    code_index = CodeIndex(
        db_path=settings.north_home / "code_index.db", embed_fn=_embed_fn, embedding_model=embedding_model
    )
    memory = LocalMemoryGateway(
        context_store=context_store,
        fact_store=fact_store,
        episodic_store=episodic_store,
    )

    tasks_db = settings.north_home / "tasks" / "tasks.db"
    agent_run_store = AgentRunStore(tasks_db)
    return Dependencies(
        context_store=context_store,
        ledger=ledger,
        inference_router=base_router,
        embedding_model=embedding_model,
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(settings.north_home / "jobs.db"),
        cost_tracker=cost_tracker,
        stream_manager=EventStreamManager(run_store=agent_run_store),
        approval_store=ApprovalStore(),
        cron_store=UserCronStore(settings.north_home / "jobs.db"),
        confidence_tracker=confidence_tracker,
        episodic_store=episodic_store,
        task_context_store=TaskContextStore(db_path=tasks_db),
        running_task_store=RunningTaskStore(settings.north_home / "running_tasks.db"),
        plan_store=PlanStore(),
        agent_run_store=agent_run_store,
        north_settings=north_settings,
        memory=memory,
        embed_fn=_embed_fn,
        fact_store=fact_store,
        code_index=code_index,
    )
