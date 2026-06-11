"""ModelDispatcher — multi-provider inference router.

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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from config.strategy import NorthSettings, StrategyMode
from inference.base import InferenceRouter
from inference.capability import ModelCapability, ModelInfo
from inference.constants import (
    _DEFAULT_MODEL_CONFIDENCE,
    _MODEL_CONFIDENCE_ALPHA,
    _MODEL_CONFIDENCE_FULL_USES,
    _MODEL_CONFIDENCE_MAX_WEIGHT,
    _QUALITY_TIER_HIGH,
    _QUALITY_TIER_MEDIUM,
)
from inference.cooldowns import CooldownStore, _CooldownKey
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    InferenceError,
    ModelRateLimitedError,
    PaymentRequiredError,
)
from inference.models import (
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
from inference.routing import _Candidate, shuffle_groups

if TYPE_CHECKING:
    from tools.confidence import ConfidenceTracker

logger = logging.getLogger(__name__)

# Seconds to batch model-confidence DB writes. Scores change on every inference
# call; writing each one individually doubles the DB traffic for no benefit.
_SCORE_FLUSH_INTERVAL_SECONDS = 30.0


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
        # (model_id, provider_name) → (ema_score, uses_count); seeded from DB at startup.
        self._model_confidence: dict[_CooldownKey, tuple[float, int]] = (
            confidence_tracker.load_model_scores_sync() if confidence_tracker is not None else {}
        )
        self._background_tasks: set[asyncio.Task] = set()
        # Scores changed since the last batched DB flush.
        self._dirty_scores: set[_CooldownKey] = set()
        self._flush_task: asyncio.Task | None = None
        self._build_registry()
        self._cooldowns.load()

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
        strategy = self._north_settings.strategy
        if strategy == StrategyMode.SPORT:
            return PoolPriority.HIGH
        if strategy == StrategyMode.ECO:
            return PoolPriority.LOW
        return requested  # CRUISE: honour caller

    # ---- InferenceRouter ABC ----

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        estimated = len(request.prompt) // 4
        candidates = self._candidates(ModelCapability.COMPLETION, self._effective_priority(request.priority), estimated)

        async def _call(provider: Provider, model_id: str) -> CompletionResponse:
            return await provider.complete(model_id, request)

        return await self._dispatch(candidates, _call)

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        text = " ".join(str(m.get("content") or "") for m in request.messages)
        # Tool schemas are sent with every request and can dwarf short
        # conversations — include them or the context-fit check undercounts.
        tools_chars = sum(len(json.dumps(t)) for t in request.tools) if request.tools else 0
        estimated = (len(text) + tools_chars) // 4
        candidates = self._candidates(ModelCapability.TOOL_CALLS, self._effective_priority(request.priority), estimated)

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

        return await self._dispatch(candidates, _call)

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
        """Blend price-based base_quality with live call success rate."""
        key: _CooldownKey = (info.model_id, info.provider_name)
        score, uses = self._model_confidence.get(key, (_DEFAULT_MODEL_CONFIDENCE, 0))
        w = min(uses / _MODEL_CONFIDENCE_FULL_USES, 1.0) * _MODEL_CONFIDENCE_MAX_WEIGHT
        return info.base_quality * (1 - w) + score * w

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

        available: list[_Candidate] = [
            (info, provider)
            for info, provider in fitting
            if not self._cooldowns.is_active((info.model_id, info.provider_name))
        ]

        # Precompute quality scores once to avoid repeated EMA calculations during sort/shuffle.
        quality: dict[_CooldownKey, float] = {
            (info.model_id, info.provider_name): self._effective_quality(info)
            for info, _ in available
        }

        if priority == PoolPriority.HIGH:
            available.sort(key=lambda x: quality[(x[0].model_id, x[0].provider_name)], reverse=True)
            available = shuffle_groups(
                available, key=lambda x: round(quality[(x[0].model_id, x[0].provider_name)], 6)
            )
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

        return available

    # ---- Dispatch ----

    async def _dispatch(
        self,
        candidates: list[_Candidate],
        call_fn: Callable[[Provider, str], Awaitable],
    ):
        if not candidates:
            raise AllModelsRateLimitedError("No models available for this request")

        for info, provider in candidates:
            key: _CooldownKey = (info.model_id, info.provider_name)
            if self._cooldowns.is_active(key):
                continue
            try:
                result = await call_fn(provider, info.model_id)
                self._record_model_outcome(key, True)
                self._persist_model_score(key)
                return result
            except ModelRateLimitedError:
                self._cooldowns.set_rate_limit(key)
                logger.info(
                    "Rate limited: %s/%s — skipping for 60 s",
                    info.provider_name,
                    info.model_id,
                )
            except PaymentRequiredError:
                self._cooldowns.set_payment_exhausted(key)
                logger.warning(
                    "Payment required: %s/%s — skipping for 24 h",
                    info.provider_name,
                    info.model_id,
                )
            except InferenceError:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                logger.warning(
                    "Inference error on %s/%s — trying next candidate",
                    info.provider_name,
                    info.model_id,
                    exc_info=True,
                )
            except Exception:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                raise

        raise AllModelsRateLimitedError(
            f"All {len(candidates)} candidate(s) exhausted — every model is rate-limited or has insufficient credits"
        )
