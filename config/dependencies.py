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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from approval import Notifier, TerminalNotifier
from approval.store import ApprovalStore
from config.settings import settings
from config.strategy import NorthSettings
from inference import InferenceRouter
from inference.exceptions import EmbeddingCountMismatchError
from inference.factory import build_router
from inference.models import EmbedFn, GlossaryFn, SupersedeFn
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter
from memory import ContextStore, SQLiteContextStore
from utils.prompts import load_prompt

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


_EMBED_CACHE_MAX_SIZE = 512


class _CachingEmbedder:
    """Embeds text once: an LRU of recent vectors plus in-flight de-duplication.

    An agent run fans out four concurrent recalls (facts, episodes, skills ×2)
    for the *same* prompt; without the in-flight map they all miss the cache and
    each pays its own round trip.
    """

    def __init__(self, cost_tracker: CostTracker) -> None:
        self._cost_tracker = cost_tracker
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[list[float]]] = {}

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        awaited: dict[str, asyncio.Future[list[float]]] = {}
        missing: list[str] = []

        for index, text in enumerate(texts):
            cached = self._cached(text)
            if cached is not None:
                results[index] = cached
                continue
            inflight = self._inflight.get(text)
            if inflight is not None:
                awaited[text] = inflight
            elif text not in awaited:
                awaited[text] = asyncio.get_running_loop().create_future()
                self._inflight[text] = awaited[text]
                missing.append(text)

        # Only the texts this call claimed are embedded; duplicates within the
        # batch and texts another coroutine is already fetching are awaited below.
        if missing:
            await self._fulfil(missing)

        for index, text in enumerate(texts):
            if results[index] is None:
                results[index] = await awaited[text]
        return [vector for vector in results if vector is not None]

    def _cached(self, text: str) -> list[float] | None:
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _remember(self, text: str, vector: list[float]) -> None:
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > _EMBED_CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

    async def _fulfil(self, texts: list[str]) -> None:
        """Embed the texts this call claimed, settling every future it created."""
        try:
            vectors = await self._embed(texts)
        except BaseException as exc:
            self._fail(texts, exc)
            raise
        for text, vector in zip(texts, vectors, strict=True):
            self._remember(text, vector)
            future = self._inflight.pop(text, None)
            if future is not None and not future.done():
                future.set_result(vector)

    def _fail(self, texts: list[str], exc: BaseException) -> None:
        for text in texts:
            future = self._inflight.pop(text, None)
            if future is not None and not future.done():
                future.set_exception(exc)
                # This caller re-raises rather than awaiting its own future, so
                # consume the result here; otherwise asyncio logs a "Future
                # exception was never retrieved" warning per text.
                future.exception()

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*, returning exactly one vector per input, in order."""
        from inference.models import EmbedRequest

        response = await self._cost_tracker.embed(EmbedRequest(texts=texts, component="embed"))
        vectors = list(response.embeddings)
        if len(vectors) != len(texts):
            # Callers zip these against their own lists (skills, tool descriptions,
            # code chunks). A short response silently shifts every later vector onto
            # the wrong item, so refuse it rather than corrupt the mapping.
            raise EmbeddingCountMismatchError(expected=len(texts), received=len(vectors))
        return vectors


def _build_supersede_fn(cost_tracker: CostTracker) -> SupersedeFn:
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
        prompt = load_prompt("prompts/fact_supersede.md").format(new_fact=new_fact, numbered=numbered)
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

    return _supersede_fn


def _build_glossary_fn(cost_tracker: CostTracker) -> GlossaryFn:
    async def _glossary_fn(context: dict[str, list[str]]) -> dict[str, str]:
        """Say what each short name means, in a few words, or nothing at all.

        Answering "no idea" has to be cheap and expected. A wrong expansion is
        spliced into every fact using that name, so silence costs one unfindable
        fact while a guess corrupts many.
        """
        from inference.models import CompletionRequest, PoolPriority
        from utils.text import extract_json

        blocks = "\n\n".join(
            f"{token}:\n" + "\n".join(f"  - {c}" for c in facts) for token, facts in context.items()
        )
        prompt = load_prompt("prompts/fact_glossary.md").format(blocks=blocks)
        response = await cost_tracker.complete(
            CompletionRequest(
                prompt=prompt, priority=PoolPriority.LOW, component="fact_glossary", json_mode=True
            )
        )
        parsed = extract_json(response.text.strip())
        raw = parsed.get("glossary") if isinstance(parsed, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {
            k: v.strip()
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip() and v.strip().lower() != "null"
        }

    return _glossary_fn


def _migrate_legacy_public_document(context_dir: Path) -> None:
    """2b memory model: public.md was renamed to user.md.

    Preserves the user's existing facts document under the new name. Idempotent.
    """
    legacy_public = context_dir / "public.md"
    user_doc = context_dir / "user.md"
    if legacy_public.exists() and not user_doc.exists():
        legacy_public.rename(user_doc)


def _default_north_settings() -> NorthSettings:
    from approval.mode import resolve_approval_mode

    return NorthSettings(
        settings.north_home / "settings.json",
        default_approval_mode=resolve_approval_mode(settings),
        default_preferred_models=_resolve_preferred_models(),
    )


def build_production_dependencies(north_settings: NorthSettings | None = None) -> Dependencies:
    """Build and wire all synchronously-constructable production dependencies."""
    from context.code_index import CodeIndex
    from inference.cost_tracker import CostTracker
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

    north_settings = north_settings or _default_north_settings()
    _migrate_legacy_public_document(settings.north_home / "context")

    context_store = SQLiteContextStore(settings.north_home / "memory.db", legacy_path=settings.north_home / "context")
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
    embed_fn: EmbedFn = _CachingEmbedder(cost_tracker)

    # Which model produced a vector decides which vectors it can be compared
    # against, so every store is stamped with it and clears itself on a change.
    embedding_model = base_router.embedding_model_id()
    episodic_store = EpisodicStore(
        db_path=settings.north_home / "episodic.db", embed_fn=embed_fn, embedding_model=embedding_model
    )
    fact_store = FactStore(
        db_path=settings.north_home / "facts.db",
        embed_fn=embed_fn,
        embedding_model=embedding_model,
        supersede_fn=_build_supersede_fn(cost_tracker),
        glossary_fn=_build_glossary_fn(cost_tracker),
    )
    code_index = CodeIndex(
        db_path=settings.north_home / "code_index.db", embed_fn=embed_fn, embedding_model=embedding_model
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
        embed_fn=embed_fn,
        fact_store=fact_store,
        code_index=code_index,
    )
