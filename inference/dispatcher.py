"""ModelDispatcher — multi-provider inference router.

Implements InferenceRouter over an ordered list of Provider instances.
Routing logic:
  1. Collect all models from all providers that satisfy the requested capability.
  2. Filter models whose context window is too small for the input.
  3. Exclude models on cooldown (rate limited or payment exhausted).
  4. Rank by priority: HIGH → quality desc, LOW → cost asc, MEDIUM → free first.
  5. Try each in order, applying cooldowns on failure, raising
     AllModelsRateLimitedError when every candidate is exhausted.

Context overflow: raises ContextTooLargeError so the agent layer can compact
the conversation and retry. See agents/context_compaction.py.

Phase 2 TODOs:
  - Track per-model task success rate and tool reliability in confidence tracker.
  - Augment base_quality with north's experience score.
  - Catch ContextTooLargeError in agents/agentic_llm_agent.py.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from config.strategy import NorthSettings, StrategyMode
from inference.base import InferenceRouter
from inference.capability import ModelCapability, ModelInfo
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    ModelRateLimitedError,
    PaymentRequiredError,
)
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.provider import Provider

logger = logging.getLogger(__name__)

_CooldownKey = tuple[str, str]  # (model_id, provider_name)


class ModelDispatcher(InferenceRouter):
    """Routes inference calls across multiple providers with per-model cooldowns."""

    _RATE_LIMIT_COOLDOWN_SECS: float = 60.0
    _PAYMENT_COOLDOWN_SECS: float = 86_400.0  # 24 h

    def __init__(
        self,
        providers: list[Provider],
        north_settings: NorthSettings | None = None,
    ) -> None:
        self._providers = providers
        self._north_settings = north_settings
        # (provider_name, model_id) → (ModelInfo, Provider)
        self._registry: dict[tuple[str, str], tuple[ModelInfo, Provider]] = {}
        self._cooldowns: dict[_CooldownKey, float] = {}
        self._build_registry()

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
        candidates = self._candidates(
            ModelCapability.COMPLETION, self._effective_priority(request.priority), estimated
        )

        async def _call(provider: Provider, model_id: str) -> CompletionResponse:
            return await provider.complete(model_id, request)

        return await self._dispatch(candidates, _call)

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        text = " ".join(str(m.get("content") or "") for m in request.messages)
        estimated = len(text) // 4
        candidates = self._candidates(
            ModelCapability.TOOL_CALLS, self._effective_priority(request.priority), estimated
        )

        async def _call(provider: Provider, model_id: str) -> ToolCallResponse:
            return await provider.complete_with_tools(model_id, request, token_callback)

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

        candidates = self._candidates(
            ModelCapability.TRANSCRIPTION, PoolPriority.MEDIUM, 0
        )

        async def _call(provider: Provider, model_id: str) -> TranscriptionResponse:
            return await provider.transcribe(model_id, request)

        return await self._dispatch(candidates, _call)

    async def get_model(self, priority: PoolPriority) -> str:
        candidates = self._candidates(ModelCapability.COMPLETION, priority, 0)
        if not candidates:
            raise AllModelsRateLimitedError("No completion models are available")
        return candidates[0][0].model_id

    async def refresh_pools(self) -> None:
        for provider in self._providers:
            try:
                await provider.refresh()
            except Exception:
                logger.warning(
                    "Pool refresh failed for provider %s", provider.name, exc_info=True
                )
        self._build_registry()

    def current_pools(self) -> dict[str, ModelPool]:
        """Build a pool snapshot from the dispatcher's own registry for CLI display.

        Maps quality tiers to the legacy pool names so the `north inference models`
        command keeps working without any knowledge of provider internals.
        """
        high: list[ModelInfo] = []
        medium: list[ModelInfo] = []
        low: list[ModelInfo] = []
        free: list[ModelInfo] = []

        for info, _ in self._registry.values():
            if not info.supports(ModelCapability.COMPLETION):
                continue
            if info.is_free:
                free.append(info)
            if info.base_quality >= 0.70:
                high.append(info)
            elif info.base_quality >= 0.40:
                medium.append(info)
            else:
                low.append(info)

        def _ids(infos: list[ModelInfo], limit: int = 10) -> list[str]:
            return [
                i.model_id
                for i in sorted(infos, key=lambda i: i.base_quality, reverse=True)[
                    :limit
                ]
            ]

        return {
            "reasoning": ModelPool(name="reasoning", models=_ids(high)),
            "fast_cheap": ModelPool(name="fast_cheap", models=_ids(medium)),
            "high_volume": ModelPool(name="high_volume", models=_ids(low)),
            "free_fallback": ModelPool(name="free_fallback", models=_ids(free)),
        }

    # ---- Internal routing ----

    def _candidates(
        self,
        capability: ModelCapability,
        priority: PoolPriority,
        estimated_tokens: int,
    ) -> list[tuple[ModelInfo, Provider]]:
        now = time.monotonic()

        capable = [
            (info, provider)
            for (info, provider) in self._registry.values()
            if info.supports(capability)
        ]
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

        available = [
            (info, provider)
            for info, provider in fitting
            if self._cooldowns.get((info.model_id, info.provider_name), 0.0) <= now
        ]

        if priority == PoolPriority.HIGH:
            # Best quality first, regardless of cost.
            available.sort(key=lambda x: x[0].base_quality, reverse=True)
        elif priority == PoolPriority.LOW:
            # Minimise resource use: free first, then smallest context window
            # (proxy for lighter/faster models), then cost ascending.
            # This stays distinct from MEDIUM even when all models are free.
            available.sort(
                key=lambda x: (
                    x[0].cost_per_token,
                    x[0].context_window if x[0].context_window > 0 else float("inf"),
                    -x[0].base_quality,
                )
            )
        else:  # MEDIUM: free models first, then by quality
            available.sort(key=lambda x: (not x[0].is_free, -x[0].base_quality))

        return available

    async def _dispatch(
        self,
        candidates: list[tuple[ModelInfo, Provider]],
        call_fn: Callable[[Provider, str], Awaitable],
    ):
        if not candidates:
            raise AllModelsRateLimitedError("No models available for this request")

        now = time.monotonic()
        for info, provider in candidates:
            key: _CooldownKey = (info.model_id, info.provider_name)
            if self._cooldowns.get(key, 0.0) > now:
                continue
            try:
                return await call_fn(provider, info.model_id)
            except ModelRateLimitedError:
                self._cooldowns[key] = (
                    time.monotonic() + self._RATE_LIMIT_COOLDOWN_SECS
                )
                logger.info(
                    "Rate limited: %s/%s — skipping for %ds",
                    info.provider_name,
                    info.model_id,
                    int(self._RATE_LIMIT_COOLDOWN_SECS),
                )
            except PaymentRequiredError:
                self._cooldowns[key] = (
                    time.monotonic() + self._PAYMENT_COOLDOWN_SECS
                )
                logger.warning(
                    "Payment required: %s/%s — skipping for 24 h",
                    info.provider_name,
                    info.model_id,
                )

        raise AllModelsRateLimitedError(
            f"All {len(candidates)} candidate(s) exhausted — every model is "
            "rate-limited or has insufficient credits"
        )
