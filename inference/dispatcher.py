"""ModelDispatcher - multi-provider inference router.

Implements InferenceRouter over an ordered list of Provider instances.
Routing logic:
  1. Collect all models from all providers that satisfy the requested capability.
  2. Filter models whose context window is too small for the input.
  3. Exclude models on cooldown (rate limited or payment exhausted).
  4. Rank by priority: HIGH → effective_quality desc, LOW → cost asc, MEDIUM → free first.
  5. Within each quality tier, candidates are shuffled randomly for uniform load distribution.
  6. Try each in order, applying cooldowns on failure, raising
     AllModelsRateLimitedError when every candidate is exhausted.

Context overflow: raises ContextTooLargeError so the agent layer can compact
the conversation and retry. See agents/context_compaction.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config.strategy import NorthSettings, StrategyMode
from inference.base import InferenceRouter
from inference.capability import ModelCapability, ModelInfo
from inference.constants import (
    _DEFAULT_MODEL_CONFIDENCE,
    _MODEL_CONFIDENCE_ALPHA,
    _PREFERRED_HEALTH_FLOOR,
    _PREFERRED_MIN_USES,
    _QUALITY_TIER_HIGH,
    _QUALITY_TIER_MEDIUM,
    _STICKY_MAX_ENTRIES,
)
from inference.cooldowns import CooldownStore, _CooldownKey
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    InferenceError,
    ModelRateLimitedError,
    PayloadTooLargeError,
    PaymentRequiredError,
    ProviderAuthError,
)
from inference.model_policy import model_matches
from inference.model_scorer import ModelScorer, ScoringConfig
from inference.models import (
    PRIORITY_TO_POOL,
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelEntry,
    ModelPool,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.provider import Provider
from inference.provider_health import ProviderHealthTracker
from inference.rate_limit_status import _PAYLOAD_TOO_LARGE_SECS, RateLimitStatusStore
from inference.routing import _Candidate, shuffle_groups
from utils.text import strip_code_fences

# Allowance (chars) for north's system prompt when estimating total request size
# for the max_payload_chars fit check. Keeps providers with tiny request caps
# (e.g. Groq free) out of candidate selection for large prompts that would 413.
_SYSTEM_PROMPT_CHARS = 8_000

if TYPE_CHECKING:
    from tools.confidence import ConfidenceTracker

logger = logging.getLogger(__name__)

# Seconds to batch model-confidence DB writes. Scores change on every inference
# call; writing each one individually doubles the DB traffic for no benefit.
_SCORE_FLUSH_INTERVAL_SECONDS = 30.0


def _completion_has_text(resp: Any) -> bool:
    """A completion is only usable if it actually returned text."""
    return bool(getattr(resp, "text", "") and resp.text.strip())


def _toolcall_has_output(resp: Any) -> bool:
    """A tool-call response is usable if it invoked tools or produced text."""
    if getattr(resp, "calls", None):
        return True
    content = getattr(resp, "content", None)
    return bool(content and content.strip())


class ModelDispatcher(InferenceRouter):
    """Routes inference calls across multiple providers with per-model cooldowns."""

    def __init__(
        self,
        providers: list[Provider],
        north_settings: NorthSettings | None = None,
        confidence_tracker: ConfidenceTracker | None = None,
        cooldowns_path: Path | None = None,
    ) -> None:
        self._providers = providers
        self._north_settings = north_settings
        self._confidence_tracker = confidence_tracker
        # (provider_name, model_id) → (ModelInfo, Provider)
        self._registry: dict[tuple[str, str], tuple[ModelInfo, Provider]] = {}
        self._cooldowns = CooldownStore(cooldowns_path)
        # Precise, provider-aware rate-limit status (persisted alongside cooldowns).
        self._rate_limit_status = RateLimitStatusStore(
            (cooldowns_path.parent / "rate_limit_status.json") if cooldowns_path is not None else None
        )
        self._provider_health = ProviderHealthTracker()
        # (model_id, provider_name) → (ema_score, uses_count); seeded from DB at startup.
        self._model_confidence: dict[_CooldownKey, tuple[float, int]] = (
            confidence_tracker.load_model_scores_sync() if confidence_tracker is not None else {}
        )
        # Scores changed since the last batched DB flush.
        self._dirty_scores: set[_CooldownKey] = set()
        self._flush_task: asyncio.Task | None = None
        # Per-task model stickiness: (task_id, component, capability, priority) →
        # (model_id, provider_name) of the first model that succeeded, reused for
        # that task's later steps. Bounded LRU (see _remember_sticky).
        self._sticky: OrderedDict[tuple[str, str, str, str], tuple[str, str]] = OrderedDict()
        self._build_registry()
        self._cooldowns.load()
        self._rate_limit_status.load()
        # Price-free quality scorer (family tier + live EMA + curation). Built
        # from the live NorthSettings so weight edits apply without a restart.
        _nh = getattr(self._north_settings, "_path", None)
        _tiers = (_nh.parent / "model_tiers.json") if _nh is not None else (Path.home() / ".north" / "model_tiers.json")
        self._scorer = ModelScorer(
            config=self._north_settings.scoring if self._north_settings is not None else ScoringConfig(),
            tiers_path=_tiers,
        )
        self._validate_preferred()

    def _build_registry(self) -> None:
        """Merge models from all providers. Each entry is keyed by (provider_name, model_id)."""
        self._registry.clear()
        for provider in self._providers:
            for model_id, info in provider.get_models().items():
                key = (info.provider_name, model_id)
                if key not in self._registry:
                    self._registry[key] = (info, provider)

    def _effective_priority(self, requested: PoolPriority) -> PoolPriority:
        """Apply the user's strategy setting to the requested priority.

        SPORT forces every call to the highest-quality pool; ECO forces every
        call to the lowest-cost pool; CRUISE (default) respects the caller.
        embed/transcribe/get_model are infrastructure calls and bypass this.
        """
        if self._north_settings is None:
            return requested
        strategy = self._north_settings.power
        if strategy == StrategyMode.SPORT:
            return PoolPriority.HIGH
        if strategy == StrategyMode.ECO:
            return PoolPriority.LOW
        return requested  # CRUISE: honour caller

    # ---- InferenceRouter ABC ----

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        estimated = len(request.prompt) // 4
        priority = self._effective_priority(request.priority)
        candidates = self._candidates(ModelCapability.COMPLETION, priority, estimated)
        candidates = self._apply_exclusions(candidates, request.exclude_models)

        async def _call(provider: Provider, model_id: str) -> CompletionResponse:
            return await provider.complete(model_id, request)

        def _valid(resp: Any) -> bool:
            # A usable completion must have text, and - when JSON was requested -
            # must actually be JSON. Models that ignore json_mode and return prose
            # or a <thought> trace are treated as failures so the dispatcher falls
            # through to a model that honours the request.
            if not _completion_has_text(resp):
                return False
            if request.json_mode:
                try:
                    json.loads(strip_code_fences(resp.text))
                except Exception:
                    return False
            return True

        sticky = self._sticky_key(request, ModelCapability.COMPLETION, priority)
        return await self._dispatch(candidates, _call, is_valid=_valid, sticky_key=sticky)

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        text = " ".join(str(m.get("content") or "") for m in request.messages)
        # Tool schemas are sent with every request and can dwarf short
        # conversations - include them or the context-fit check undercounts.
        tools_chars = sum(len(json.dumps(t)) for t in request.tools) if request.tools else 0
        estimated = (len(text) + tools_chars) // 4
        priority = self._effective_priority(request.priority)
        candidates = self._candidates(ModelCapability.TOOL_CALLS, priority, estimated)
        candidates = self._apply_exclusions(candidates, request.exclude_models)

        forwarded = False
        wrapped_cb: Callable[[str], Awaitable[None]] | None = None
        if token_callback is not None:

            async def wrapped_cb(token: str) -> None:
                nonlocal forwarded
                forwarded = True
                await token_callback(token)

        async def _call(provider: Provider, model_id: str) -> ToolCallResponse:
            nonlocal forwarded
            if forwarded:
                # A previous candidate streamed partial output before failing.
                # Ask the UI to discard it (when the callback supports reset)
                # so the re-streamed answer isn't shown twice.
                reset = getattr(token_callback, "reset", None)
                if reset is not None:
                    await reset()
                forwarded = False
            return await provider.complete_with_tools(model_id, request, wrapped_cb)

        sticky = self._sticky_key(request, ModelCapability.TOOL_CALLS, priority)
        return await self._dispatch(candidates, _call, is_valid=_toolcall_has_output, sticky_key=sticky)

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        candidates = self._candidates(ModelCapability.EMBEDDING, PoolPriority.MEDIUM, 0)

        async def _call(provider: Provider, model_id: str) -> EmbedResponse:
            return await provider.embed(model_id, request)

        return await self._dispatch(candidates, _call)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        if request.model:
            for (_, mid), (info, provider) in self._registry.items():
                if mid == request.model and info.supports(ModelCapability.TRANSCRIPTION):
                    return await provider.transcribe(mid, request)

        candidates = self._candidates(ModelCapability.TRANSCRIPTION, PoolPriority.MEDIUM, 0)

        async def _call(provider: Provider, model_id: str) -> TranscriptionResponse:
            return await provider.transcribe(model_id, request)

        return await self._dispatch(candidates, _call)

    async def get_model(self, priority: PoolPriority) -> str:
        candidates = self._candidates(ModelCapability.COMPLETION, priority, 0)
        if not candidates:
            raise AllModelsRateLimitedError("No completion models are available")
        return candidates[0][0].model_id

    async def aclose(self) -> None:
        """Close all provider HTTPX clients. Call on application shutdown."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        await self._flush_dirty_scores()  # don't lose scores batched since the last flush
        for provider in self._providers:
            if hasattr(provider, "aclose"):
                await provider.aclose()

    async def refresh_pools(self) -> None:
        for provider in self._providers:
            try:
                await provider.refresh()
            except Exception:
                logger.warning("Pool refresh failed for provider %s", provider.name, exc_info=True)
        self._build_registry()
        self._validate_preferred()

    def rebuild(self, providers: list[Provider]) -> None:
        """Swap in a fresh provider list in place (no restart needed).

        Used by runtime config changes (e.g. north_config set NORTH_*_API_KEY)
        so the dispatcher picks up new providers without rebuilding the whole
        dependency graph. The existing registry/cooldowns/confidence state are
        preserved; only the provider set and derived registry are replaced.
        """
        # Close outgoing provider HTTPX clients to avoid socket leaks.
        for provider in self._providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    _ = close()
                except Exception:
                    logger.warning("Failed to close provider %s on rebuild", provider.name, exc_info=True)
        self._providers = providers
        self._build_registry()
        self._validate_preferred()

    def rate_limit_status(self) -> list[dict]:
        """Snapshot of currently-unavailable (provider, model) pairs.

        Each entry has the precise reset time, the provider's own wait signal,
        the tier (free/paid), and the limit/remaining when the provider sent
        them. Used by ``north limits`` and the status API.
        """
        return [r.to_dict() for r in self._rate_limit_status.snapshot()]

    def rate_limit_status_summary(self) -> dict[str, int | None]:
        """Counts for the status formatter's 'unknown vs verified' distinction.

        ``checked`` = models used successfully this session; ``pool_total`` = total
        completion-capable models in the registry. Lets the UI report how many models
        have never been probed (and are therefore 'unknown', not 'available').
        """
        pool_total = sum(
            1 for info, _ in self._registry.values() if info.supports(ModelCapability.COMPLETION)
        )
        return {"checked": self._rate_limit_status.checked_count(), "pool_total": pool_total}

    def current_pools(self) -> dict[str, ModelPool]:
        """Build a pool snapshot from the dispatcher's own registry for CLI display."""
        high: list[ModelInfo] = []
        medium: list[ModelInfo] = []
        low: list[ModelInfo] = []
        free: list[ModelInfo] = []

        for info, _ in self._registry.values():
            if not info.supports(ModelCapability.COMPLETION):
                continue
            if info.is_free:
                free.append(info)
            if info.base_quality >= _QUALITY_TIER_HIGH:
                high.append(info)
            elif info.base_quality >= _QUALITY_TIER_MEDIUM:
                medium.append(info)
            else:
                low.append(info)

        def _entries(infos: list[ModelInfo]) -> list[ModelEntry]:
            return [
                ModelEntry(id=i.model_id, provider=i.provider_name)
                for i in sorted(infos, key=lambda i: i.base_quality, reverse=True)
            ]

        return {
            "reasoning": ModelPool(name="reasoning", models=_entries(high)),
            "fast_cheap": ModelPool(name="fast_cheap", models=_entries(medium)),
            "high_volume": ModelPool(name="high_volume", models=_entries(low)),
            "free_fallback": ModelPool(name="free_fallback", models=_entries(free)),
        }

    # ---- EMA confidence tracking ----

    def _record_model_outcome(self, key: _CooldownKey, success: bool) -> None:
        prev_score, prev_uses = self._model_confidence.get(key, (_DEFAULT_MODEL_CONFIDENCE, 0))
        outcome = 1.0 if success else 0.0
        new_score = max(
            0.0,
            min(1.0, _MODEL_CONFIDENCE_ALPHA * outcome + (1 - _MODEL_CONFIDENCE_ALPHA) * prev_score),
        )
        self._model_confidence[key] = (new_score, prev_uses + 1)

    def _persist_model_score(self, key: _CooldownKey) -> None:
        """Mark a score dirty and schedule a debounced flush.

        Scores are written in one batch every _SCORE_FLUSH_INTERVAL_SECONDS
        instead of one DB write per inference call.
        """
        if self._confidence_tracker is None:
            return
        self._dirty_scores.add(key)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_scores_after_delay())

    async def _flush_scores_after_delay(self) -> None:
        await asyncio.sleep(_SCORE_FLUSH_INTERVAL_SECONDS)
        await self._flush_dirty_scores()

    async def _flush_dirty_scores(self) -> None:
        if self._confidence_tracker is None or not self._dirty_scores:
            return
        dirty, self._dirty_scores = self._dirty_scores, set()
        for key in dirty:
            score, uses = self._model_confidence.get(key, (None, None))
            if score is None:
                continue
            try:
                await self._confidence_tracker.save_model_score(key[0], key[1], score, uses)
            except Exception:
                logger.warning("Failed to persist model score for %s/%s", key[1], key[0], exc_info=True)

    def _effective_quality(self, info: ModelInfo) -> float:
        """Price-free quality score: family tier + live EMA + curation boost.

        Replaces the old price-derived base_quality blend. When all providers
        are free, price carries no signal; this scorer uses a static family
        prior plus the live per-model success EMA so a 7B and a 200B model are
        no longer treated as equals.
        """
        key: _CooldownKey = (info.model_id, info.provider_name)
        score, uses = self._model_confidence.get(key, (_DEFAULT_MODEL_CONFIDENCE, 0))
        is_preferred = self._is_preferred(info, key)
        power_mode = self._north_settings.power if self._north_settings is not None else StrategyMode.CRUISE
        return self._scorer.score(
            model_id=info.model_id,
            ema_score=score,
            is_preferred=is_preferred,
            power=power_mode,
        )

    def reload_scoring(self) -> None:
        """Re-read scoring weights + family-tier overrides without a restart.

        Called by the live-config-reload path (north_config tool / _apply_runtime)
        so weight edits in settings.json and ~/.north/model_tiers.json take effect
        on the running dispatcher immediately.
        """
        if self._north_settings is not None:
            self._scorer.set_config(self._north_settings.scoring)
        self._scorer.reload()

    # ---- Candidate selection ----

    def _candidates(
        self,
        capability: ModelCapability,
        priority: PoolPriority,
        estimated_tokens: int,
    ) -> list[_Candidate]:
        capable = [pair for pair in self._registry.values() if pair[0].supports(capability)]
        if not capable:
            return []

        if estimated_tokens > 0:
            fitting = [
                (info, provider)
                for info, provider in capable
                # context_window == 0 means "not applicable" (e.g. transcription)
                if info.context_window == 0 or info.context_window >= estimated_tokens
            ]
            if not fitting:
                largest = max(
                    (i.context_window for i, _ in capable if i.context_window > 0),
                    default=0,
                )
                raise ContextTooLargeError(estimated_tokens, largest)
        else:
            fitting = capable

        # Payload-size fit: a model with a max_payload_chars cap is skipped when the
        # estimated total request (user prompt + a system-prompt allowance) exceeds it.
        # This routes large north prompts (planner system prompt + context) away from
        # providers with tiny request caps (e.g. Groq free, which 413s) toward models
        # that accept the payload. request_chars is None when the prompt is empty.
        if estimated_tokens > 0:
            estimated_payload_chars = estimated_tokens * 4 + _SYSTEM_PROMPT_CHARS
            fitting = [
                (info, provider)
                for info, provider in fitting
                if info.max_payload_chars is None or estimated_payload_chars <= info.max_payload_chars
            ]

        available: list[_Candidate] = [
            (info, provider)
            for info, provider in fitting
            if not self._cooldowns.is_active((info.model_id, info.provider_name))
            and self._provider_health.is_available(info.provider_name)
        ]

        # Precompute quality scores once to avoid repeated EMA calculations during sort/shuffle.
        quality: dict[_CooldownKey, float] = {
            (info.model_id, info.provider_name): self._effective_quality(info) for info, _ in available
        }

        if priority == PoolPriority.HIGH:
            available.sort(key=lambda x: quality[(x[0].model_id, x[0].provider_name)], reverse=True)
            available = shuffle_groups(available, key=lambda x: round(quality[(x[0].model_id, x[0].provider_name)], 6))
        elif priority == PoolPriority.LOW:
            available.sort(
                key=lambda x: (
                    x[0].cost_per_token,
                    x[0].context_window if x[0].context_window > 0 else float("inf"),
                    -quality[(x[0].model_id, x[0].provider_name)],
                )
            )
            available = shuffle_groups(
                available,
                key=lambda x: (
                    x[0].cost_per_token,
                    x[0].context_window if x[0].context_window > 0 else float("inf"),
                ),
            )
        else:  # MEDIUM: free models first, shuffle within each free/paid tier.
            available.sort(key=lambda x: (not x[0].is_free, -quality[(x[0].model_id, x[0].provider_name)]))
            available = shuffle_groups(
                available,
                key=lambda x: (not x[0].is_free, round(quality[(x[0].model_id, x[0].provider_name)], 6)),
            )

        return self._promote_preferred(available, capability, priority)

    # ---- Preferred-model promotion ----

    def _apply_exclusions(self, candidates: list[_Candidate], exclude_models: list[str]) -> list[_Candidate]:
        """Drop excluded model ids from candidates, degrading gracefully.

        Used to force an independent model choice (e.g. a reviewer that must not
        reuse the coder's model). If excluding would leave no candidates - the
        excluded model is the only one available - the exclusion is skipped and a
        warning logged, so a task is never blocked on model scarcity (the DoD gate
        then honestly flags the review as non-independent).
        """
        if not exclude_models:
            return candidates
        excluded = {m.strip().lower() for m in exclude_models if m.strip()}
        if not excluded:
            return candidates
        filtered = [(i, p) for i, p in candidates if i.model_id.lower() not in excluded]
        if not filtered:
            logger.warning(
                "exclude_models %s left no candidates - proceeding without exclusion "
                "(an independent second opinion is not possible right now)",
                sorted(excluded),
            )
            return candidates
        return filtered

    def _is_preferred(self, info: ModelInfo, key: _CooldownKey) -> bool:
        """True if the model matches any curated preferred spec.

        Used as a light ranking nudge by the scorer; health-gated promotion is
        handled separately in _promote_preferred.
        """
        if self._north_settings is None:
            return False
        for specs in self._north_settings.preferred_models.values():
            for spec in specs:
                if model_matches(spec, info.provider_name, info.model_id):
                    return True
        return False

    def _preferred_specs(self, pool: str | None) -> list[str]:
        """Curated preferred specs for a pool, from live settings (empty if unset)."""
        if self._north_settings is None or not pool:
            return []
        try:
            return self._north_settings.preferred_models.get(pool, [])
        except Exception:
            return []

    def _preferred_healthy(self, key: _CooldownKey) -> bool:
        """False once a preferred model has enough failed history to stop promoting it.

        A preferred model that keeps failing (EMA below the floor after enough
        uses) drops back to its normal price position instead of being retried
        first on every call - so a bad preference can't pin errors/latency to the
        front of the queue. Its cooldown/fallback handling is otherwise unchanged.
        """
        score, uses = self._model_confidence.get(key, (_DEFAULT_MODEL_CONFIDENCE, 0))
        return not (uses >= _PREFERRED_MIN_USES and score < _PREFERRED_HEALTH_FLOOR)

    def _promote_preferred(
        self, available: list[_Candidate], capability: ModelCapability, priority: PoolPriority
    ) -> list[_Candidate]:
        """Move available, healthy preferred models to the front in curated order.

        The price-ranked remainder is kept behind as a resilient fallback, so a
        stale/unavailable preference never blocks a call - it simply degrades to
        today's behaviour. Only applies to chat/tool-call routing; embeddings and
        transcription are unaffected. The preferred front is deterministic (not
        shuffled) so a coding task is done by a consistent model.
        """
        if capability not in (ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS):
            return available
        specs = self._preferred_specs(PRIORITY_TO_POOL.get(priority))
        if not specs:
            return available

        front: list[_Candidate] = []
        seen: set[_CooldownKey] = set()
        for spec in specs:
            for info, provider in available:
                key: _CooldownKey = (info.model_id, info.provider_name)
                if key in seen:
                    continue
                if model_matches(spec, info.provider_name, info.model_id) and self._preferred_healthy(key):
                    front.append((info, provider))
                    seen.add(key)
        if not front:
            # Preferred configured but none available/healthy right now: fall back
            # to price ranking. Kept at debug so a busy pool doesn't spam logs.
            logger.debug("preferred: no preferred model available for priority %s - using price fallback", priority)
            return available
        rest = [(i, p) for i, p in available if (i.model_id, i.provider_name) not in seen]
        return front + rest

    def _validate_preferred(self) -> None:
        """Warn when a curated preferred spec matches no model in the live catalog.

        Runs at startup and after every pool refresh so a stale/renamed model id
        is surfaced (and then silently falls back to price ranking) instead of
        appearing configured while actually routing by price.
        """
        if self._north_settings is None:
            return
        try:
            preferred = self._north_settings.preferred_models
        except Exception:
            return
        registry = list(self._registry.values())
        if not registry:
            # Catalog not fetched yet (providers populate on the first async
            # refresh_pools()); skip now to avoid warning about every spec, and
            # re-validate once the live catalog is loaded.
            return
        for pool, specs in preferred.items():
            for spec in specs:
                if not any(model_matches(spec, info.provider_name, info.model_id) for info, _ in registry):
                    logger.warning(
                        "preferred model %r (pool %s) matches no model in the live catalog - "
                        "it will be skipped until it appears; check the id or your provider keys",
                        spec,
                        pool,
                    )

    # ---- Per-task model stickiness ----

    @staticmethod
    def _sticky_key(
        request: CompletionRequest | ToolCallRequest, capability: ModelCapability, priority: PoolPriority
    ) -> tuple[str, str, str, str] | None:
        """Stickiness key for a request, or None when there is no task to scope to."""
        task_id = getattr(request, "task_id", None)
        if not task_id:
            return None
        component = getattr(request, "component", "") or ""
        return (task_id, component, capability.value, priority.value)

    def _apply_stickiness(
        self, sticky_key: tuple[str, str, str, str], candidates: list[_Candidate]
    ) -> list[_Candidate]:
        """Move this task's already-chosen model to the front if it's still a candidate.

        If the pinned model is absent now (on cooldown, dropped from the catalog,
        or no longer fits the context) the list is returned unchanged and normal
        preferred/price ordering applies - the pin is refreshed on the next success.
        """
        pinned = self._sticky.get(sticky_key)
        if pinned is None:
            return candidates
        for idx, (info, _) in enumerate(candidates):
            if (info.model_id, info.provider_name) == pinned:
                if idx == 0:
                    return candidates
                return [candidates[idx], *candidates[:idx], *candidates[idx + 1 :]]
        return candidates

    def _remember_sticky(self, sticky_key: tuple[str, str, str, str], key: _CooldownKey) -> None:
        """Record the model that just succeeded for this task; bounded LRU eviction."""
        prev = self._sticky.get(sticky_key)
        if prev is not None and prev != key:
            logger.info(
                "model switched for task %s/%s: %s → %s",
                sticky_key[0],
                sticky_key[1],
                prev[0],
                key[0],
            )
        self._sticky[sticky_key] = key
        self._sticky.move_to_end(sticky_key)
        while len(self._sticky) > _STICKY_MAX_ENTRIES:
            self._sticky.popitem(last=False)

    # ---- Dispatch ----

    async def _dispatch(
        self,
        candidates: list[_Candidate],
        call_fn: Callable[[Provider, str], Awaitable],
        is_valid: Callable[[Any], bool] | None = None,
        sticky_key: tuple[str, str, str, str] | None = None,
    ):
        if not candidates:
            raise AllModelsRateLimitedError("No models available for this request")

        if sticky_key is not None:
            candidates = self._apply_stickiness(sticky_key, candidates)

        for info, provider in candidates:
            key: _CooldownKey = (info.model_id, info.provider_name)
            if self._cooldowns.is_active(key):
                continue
            if not self._provider_health.is_available(info.provider_name):
                continue
            try:
                result = await call_fn(provider, info.model_id)
                # A model that returns an empty/degenerate response (200 OK but no
                # usable content) must not count as success - otherwise a single
                # broken model in the pool silently breaks every caller. Treat it
                # like a failure: deprioritise it and fall through to the next.
                if is_valid is not None and not is_valid(result):
                    self._record_model_outcome(key, False)
                    self._persist_model_score(key)
                    self._cooldowns.set_rate_limit(key, 120)
                    logger.warning(
                        "Empty/invalid response from %s/%s - trying next candidate",
                        info.provider_name,
                        info.model_id,
                    )
                    continue
                self._record_model_outcome(key, True)
                self._persist_model_score(key)
                self._provider_health.record_success(info.provider_name)
                self._rate_limit_status.mark_ok(info.provider_name, info.model_id)
                if sticky_key is not None:
                    self._remember_sticky(sticky_key, key)
                return result
            except ModelRateLimitedError as e:
                self._cooldowns.set_rate_limit(key, e.retry_after)
                self._rate_limit_status.record_rate_limit(
                    info.provider_name,
                    info.model_id,
                    status_code=e.status_code,
                    headers=e.headers,
                    body=e.body,
                    retry_after=e.retry_after,
                    is_free=info.is_free,
                )
                logger.info(
                    "Rate limited: %s/%s - skipping for %s",
                    info.provider_name,
                    info.model_id,
                    f"{e.retry_after:.0f}s (Retry-After)" if e.retry_after else "60 s",
                )
            except PaymentRequiredError:
                self._cooldowns.set_payment_exhausted(key)
                self._rate_limit_status.record_payment_required(
                    info.provider_name,
                    info.model_id,
                    is_free=info.is_free,
                )
                logger.warning(
                    "Payment required: %s/%s - skipping for 24 h",
                    info.provider_name,
                    info.model_id,
                )
            except PayloadTooLargeError:
                # 413: this model can't accept north's request size. Skip it (1h) and
                # route to a model that accepts the payload, rather than retrying.
                self._cooldowns.set_rate_limit(key, _PAYLOAD_TOO_LARGE_SECS)
                self._rate_limit_status.record_payload_too_large(
                    info.provider_name,
                    info.model_id,
                    is_free=info.is_free,
                )
                logger.warning(
                    "Payload too large: %s/%s - skipping for 1h",
                    info.provider_name,
                    info.model_id,
                )
            except ProviderAuthError:
                self._provider_health.mark_down(info.provider_name, "provider auth failed")
                self._rate_limit_status.record_provider_down(
                    info.provider_name, "provider auth failed"
                )
                logger.warning(
                    "Provider auth failed: %s/%s - skipping provider for 24 h",
                    info.provider_name,
                    info.model_id,
                )
            except InferenceError as e:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                state = self._provider_health.mark_degraded(info.provider_name, "inference error")
                if state != "healthy":
                    logger.warning(
                        "Provider degraded: %s/%s - circuit %s",
                        info.provider_name,
                        info.model_id,
                        state,
                    )
                # Surface the failure in the status store so 'north limits' / '/limits'
                # show it instead of a false "all available" (covers 5xx, timeouts,
                # bad-JSON, transcription failures - anything raising InferenceError).
                self._rate_limit_status.record_error(
                    info.provider_name,
                    info.model_id,
                    reason=str(e)[:160] or "inference error",
                    is_free=info.is_free,
                )
                logger.warning(
                    "Inference error on %s/%s - trying next candidate",
                    info.provider_name,
                    info.model_id,
                    exc_info=True,
                )
            except Exception:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                raise

        raise AllModelsRateLimitedError(
            f"All {len(candidates)} candidate(s) exhausted - every model is rate-limited or has insufficient credits"
        )
