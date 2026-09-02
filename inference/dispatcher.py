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
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    PayloadTooLargeError,
    PaymentRequiredError,
    ProviderAuthError,
    ProviderUnavailableError,
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
from utils.text import extract_json

# Allowance (chars) for north's system prompt when estimating total request size
# for the max_payload_chars fit check. Keeps providers with tiny request caps
# (e.g. Groq free) out of candidate selection for large prompts that would 413.
_SYSTEM_PROMPT_CHARS = 8_000

# Assumed window for a model absent from the live catalog (an id the provider
# reported for a call but not in /models). Conservative enough that compaction
# still triggers before a genuinely smaller model overflows.
_DEFAULT_CONTEXT_WINDOW = 128_000

if TYPE_CHECKING:
    from tools.confidence import ConfidenceTracker

logger = logging.getLogger(__name__)

# Seconds to batch model-confidence DB writes. Scores change on every inference
# call; writing each one individually doubles the DB traffic for no benefit.
_SCORE_FLUSH_INTERVAL_SECONDS = 30.0


# Cooldown key for "this model cannot produce structured output". Kept separate
# from ModelCapability because it is not a selection filter - any completion
# model may be able to do it, and the only way to find out is to ask.
_STRUCTURED_OUTPUT = "structured_output"


def _completion_has_text(resp: Any) -> bool:
    """A completion is only usable if it actually returned text."""
    return bool(getattr(resp, "text", "") and resp.text.strip())


def _satisfies_json_request(text: str, request: CompletionRequest) -> bool:
    """True when *text* is the JSON this request asked for, not merely some JSON.

    Lenient about form: a free model that cannot honour ``response_format`` often
    still returns the right JSON as plain text, and discarding it would waste a
    working model. Strict about substance: see ``CompletionRequest.matches_schema``.
    """
    try:
        parsed = extract_json(text)
    except Exception:
        return False
    return request.matches_schema(parsed)


def _toolcall_has_output(resp: Any) -> bool:
    """A tool-call response is usable if it invoked tools, produced text, or reasoning."""
    if getattr(resp, "calls", None):
        return True
    content = getattr(resp, "content", None)
    if bool(content and content.strip()):
        return True
    reasoning = getattr(resp, "reasoning", None)
    return bool(reasoning and reasoning.strip())


class _Deferred:
    """A candidate list built only if it is actually needed, then memoised.

    The free-tier fallback is consulted only when the primary chain is exhausted,
    which is the rare path - but building it costs a full registry scan, a quality
    score per model, a sort and a shuffle. Wrapping it keeps that cost off every
    successful call while leaving the exhaustion path unchanged.
    """

    __slots__ = ("_build", "_value")

    def __init__(self, build: Callable[[], list[_Candidate]]) -> None:
        self._build = build
        self._value: list[_Candidate] | None = None

    def get(self) -> list[_Candidate]:
        if self._value is None:
            self._value = self._build()
        return self._value


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
        # Bumped whenever the registry is rebuilt (a pool refresh or provider swap),
        # which is the only thing that invalidates a cached candidate list.
        self._generation: int = 0
        self._candidate_cache: dict[tuple, tuple[int, list[_Candidate]]] = {}
        self._context_windows: dict[str, int] = {}
        self._context_windows_normalised: dict[str, int] = {}
        # Provider catalogues are fetched over the network after construction, so
        # until the first refresh_pools() returns an empty registry means "not
        # loaded yet" rather than "no models available".
        self._catalog_refreshed: bool = False
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
        self._index_registry()

    def _index_registry(self) -> None:
        """Rebuild the lookups derived from the registry.

        Bumps the generation so any per-call candidate cache keyed on it is
        invalidated in one step, and rebuilds the context-window tables that
        `get_context_window` reads on every turn.
        """
        self._generation += 1
        self._candidate_cache.clear()
        self._context_windows = {}
        self._context_windows_normalised = {}
        # Keyed off info.model_id, not the registry key: every other read path uses
        # .values(), so the value is the authoritative identity here.
        for info, _provider in self._registry.values():
            if info.context_window <= 0:
                continue
            self._context_windows.setdefault(info.model_id, info.context_window)
            self._context_windows_normalised.setdefault(info.model_id.lower().strip(), info.context_window)

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
        candidates = self._candidates(ModelCapability.COMPLETION, priority, estimated, pool=request.pool)
        # Deferred: building the free-tier list is a full registry scan, score, sort
        # and shuffle, and it is only consulted when the primary chain is exhausted.
        fallback = _Deferred(lambda: self._free_fallback_candidates(ModelCapability.COMPLETION, estimated))
        if not candidates:
            # Primary pool exhausted (out of credits / all rate-limited / down) -
            # fall back to the free tier so the request still completes.
            candidates = fallback.get()
            fallback = None
        candidates = self._apply_exclusions(candidates, request.exclude_models)

        async def _call(provider: Provider, model_id: str) -> CompletionResponse:
            return await provider.complete(model_id, request)

        def _valid(resp: Any) -> bool:
            # A usable completion must have text, and - when JSON was requested by
            # either mechanism - must actually be the JSON that was asked for.
            # Models that ignore the request and return prose or a <thought> trace
            # are treated as failures so the dispatcher falls through to a model
            # that honours it.
            if not _completion_has_text(resp):
                return False
            if request.wants_json:
                return _satisfies_json_request(resp.text, request)
            return True

        # A model that cannot produce structured output is usually fine at plain
        # completion, so failures here suspend STRUCTURED_OUTPUT rather than
        # COMPLETION - otherwise one JSON request would evict a good chat model
        # from the general pool for an hour.
        capability = _STRUCTURED_OUTPUT if request.wants_json else ModelCapability.COMPLETION
        return await self._dispatch(
            candidates,
            _call,
            is_valid=_valid,
            sticky_key=self._sticky_key(request, capability, priority),
            fallback_candidates=fallback,
            capability=capability,
        )

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        # Estimate tokens safely without treating base64 image data URLs as millions of text tokens
        estimated = 0
        for m in request.messages:
            c = m.get("content")
            if isinstance(c, str):
                estimated += len(c) // 4
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        if part.get("type") == "image_url":
                            estimated += 1200  # Standard high-res image token footprint
                        elif part.get("type") == "text":
                            estimated += len(str(part.get("text") or "")) // 4
                        else:
                            estimated += len(str(part)) // 4
                    else:
                        estimated += len(str(part)) // 4
            elif c:
                estimated += len(str(c)) // 4
        if request.tools:
            tools_chars = sum(len(json.dumps(t)) for t in request.tools)
            estimated += tools_chars // 4
        priority = self._effective_priority(request.priority)
        candidates = self._candidates(ModelCapability.TOOL_CALLS, priority, estimated, pool=request.pool)
        fallback = _Deferred(lambda: self._free_fallback_candidates(ModelCapability.TOOL_CALLS, estimated))
        if not candidates:
            candidates = fallback.get()
            fallback = None
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
        return await self._dispatch(
            candidates,
            _call,
            is_valid=_toolcall_has_output,
            sticky_key=sticky,
            fallback_candidates=fallback,
            capability=ModelCapability.TOOL_CALLS,
        )

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        candidates = self._candidates(ModelCapability.EMBEDDING, PoolPriority.MEDIUM, 0)

        async def _call(provider: Provider, model_id: str) -> EmbedResponse:
            return await provider.embed(model_id, request)

        return await self._dispatch(candidates, _call, capability=ModelCapability.EMBEDDING)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        if request.model:
            for (_, mid), (info, provider) in self._registry.items():
                if mid == request.model and info.supports(ModelCapability.TRANSCRIPTION):
                    return await provider.transcribe(mid, request)

        candidates = self._candidates(ModelCapability.TRANSCRIPTION, PoolPriority.MEDIUM, 0)

        async def _call(provider: Provider, model_id: str) -> TranscriptionResponse:
            return await provider.transcribe(model_id, request)

        return await self._dispatch(candidates, _call, capability=ModelCapability.TRANSCRIPTION)

    async def get_model(self, priority: PoolPriority) -> str:
        candidates = self._candidates(ModelCapability.COMPLETION, priority, 0)
        if not candidates:
            candidates = self._free_fallback_candidates(ModelCapability.COMPLETION, 0)
        if not candidates:
            raise AllModelsRateLimitedError("No completion models are available")
        return candidates[0][0].model_id

    def get_context_window(self, model_id: str) -> int:
        """Return the published context window (tokens) for model_id from the live registry.

        Called once per ReAct turn by context compaction, so exact and normalised
        lookups come from a table built with the registry rather than two linear
        scans over every known model.
        """
        if not model_id:
            return _DEFAULT_CONTEXT_WINDOW

        window = self._context_windows.get(model_id)
        if window:
            return window

        norm = model_id.lower().strip()
        window = self._context_windows_normalised.get(norm)
        if window:
            return window

        # Suffix match (e.g. "openai/gpt-4o" against a registry entry "gpt-4o").
        # Rare, so the scan stays here rather than in the indexed path.
        for reg_norm, reg_window in self._context_windows_normalised.items():
            if norm.endswith(reg_norm) or reg_norm.endswith(norm):
                return reg_window

        return _DEFAULT_CONTEXT_WINDOW

    async def aclose(self) -> None:
        """Close all provider HTTPX clients. Call on application shutdown."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        await self._flush_dirty_scores()  # don't lose scores batched since the last flush
        for provider in self._providers:
            if hasattr(provider, "aclose"):
                await provider.aclose()

    async def refresh_pools(self) -> None:
        """Concurrently fetch model lists from all providers and atomically rebuild registry."""
        results = await asyncio.gather(
            *(provider.refresh() for provider in self._providers),
            return_exceptions=True,
        )
        for provider, res in zip(self._providers, results, strict=False):
            if isinstance(res, Exception):
                logger.warning(
                    "Pool refresh failed for provider %s: %s (retaining existing catalog)",
                    provider.name,
                    res,
                )
        self._build_registry()
        # The first refresh has now finished, whether or not any provider
        # answered. An empty catalogue from here on is a real fault, not a
        # not-yet-loaded one - see health_summary().
        self._catalog_refreshed = True
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
                    res = close()
                    if asyncio.iscoroutine(res):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            pass
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
        pool_total = sum(1 for info, _ in self._registry.values() if info.supports(ModelCapability.COMPLETION))
        return {"checked": self._rate_limit_status.checked_count(), "pool_total": pool_total}

    def current_pools(self) -> dict[str, ModelPool]:
        """Build a pool snapshot from the dispatcher's own registry across capability pools."""
        reasoning: list[ModelInfo] = []
        speed: list[ModelInfo] = []
        tools: list[ModelInfo] = []
        vision: list[ModelInfo] = []
        audio: list[ModelInfo] = []
        embeddings: list[ModelInfo] = []
        free: list[ModelInfo] = []
        low: list[ModelInfo] = []

        for info, _ in self._registry.values():
            if info.is_free:
                free.append(info)
            if info.supports(ModelCapability.REASONING) or info.base_quality >= _QUALITY_TIER_HIGH:
                reasoning.append(info)
            if info.supports(ModelCapability.SPEED) or (_QUALITY_TIER_MEDIUM <= info.base_quality < _QUALITY_TIER_HIGH):
                speed.append(info)
            if info.supports(ModelCapability.TOOL_CALLS):
                tools.append(info)
            if info.supports(ModelCapability.VISION):
                vision.append(info)
            if info.supports(ModelCapability.AUDIO) or info.supports(ModelCapability.TRANSCRIPTION):
                audio.append(info)
            if info.supports(ModelCapability.EMBEDDING):
                embeddings.append(info)
            if info.base_quality < _QUALITY_TIER_MEDIUM:
                low.append(info)

        def _entries(infos: list[ModelInfo]) -> list[ModelEntry]:
            return [
                ModelEntry(id=i.model_id, provider=i.provider_name)
                for i in sorted(infos, key=lambda i: i.base_quality, reverse=True)
            ]

        return {
            "reasoning": ModelPool(name="reasoning", models=_entries(reasoning)),
            "speed": ModelPool(name="speed", models=_entries(speed)),
            "tool_calling": ModelPool(name="tool_calling", models=_entries(tools)),
            "vision": ModelPool(name="vision", models=_entries(vision)),
            "audio": ModelPool(name="audio", models=_entries(audio)),
            "embeddings": ModelPool(name="embeddings", models=_entries(embeddings)),
            "fast_cheap": ModelPool(name="fast_cheap", models=_entries(speed)),
            "high_volume": ModelPool(name="high_volume", models=_entries(low)),
            "free_fallback": ModelPool(name="free_fallback", models=_entries(free)),
        }

    def health_summary(self) -> dict[str, int | bool]:
        """Report models that are usable after provider and cooldown checks."""
        completion = [info for info, _provider in self._registry.values() if info.supports(ModelCapability.COMPLETION)]
        available = [
            info
            for info in completion
            if self._provider_health.is_available(info.provider_name)
            and not self._cooldowns.is_active((info.model_id, info.provider_name))
        ]
        return {
            "ready": bool(available),
            "models": len(available),
            "providers": len({info.provider_name for info in available}),
            "catalog_loaded": self._catalog_refreshed,
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
        while True:
            await asyncio.sleep(_SCORE_FLUSH_INTERVAL_SECONDS)
            # Flush before deciding to stop: checking first left any score marked
            # dirty between the check and the task ending unwritten until the next
            # call happened to restart the loop.
            await self._flush_dirty_scores()
            if not self._dirty_scores:
                break

    async def _flush_dirty_scores(self) -> None:
        if self._confidence_tracker is None or not self._dirty_scores:
            return
        dirty, self._dirty_scores = self._dirty_scores, set()
        items: list[tuple[str, str, float, int]] = []
        for key in dirty:
            score, uses = self._model_confidence.get(key, (None, None))
            if score is not None and uses is not None:
                items.append((key[0], key[1], score, uses))
        if items:
            try:
                if hasattr(self._confidence_tracker, "save_model_scores_batch"):
                    await self._confidence_tracker.save_model_scores_batch(items)
                else:
                    for model_id, provider, score, uses in items:
                        await self._confidence_tracker.save_model_score(model_id, provider, score, uses)
            except Exception:
                self._dirty_scores.update(dirty)
                logger.warning("Failed to persist model scores batch (%d items)", len(items), exc_info=True)

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

    def _capability_supported(self, req_cap: ModelCapability, capability: ModelCapability) -> list[_Candidate]:
        """Registry entries supporting both capabilities, cached per generation.

        Pure over the registry, so it is rebuilt only when the catalog is (a pool
        refresh or a provider swap) rather than rescanned on every inference call.
        """
        cache_key = (req_cap, capability)
        cached = self._candidate_cache.get(cache_key)
        if cached is not None and cached[0] == self._generation:
            return cached[1]
        supported = [
            pair for pair in self._registry.values() if pair[0].supports(req_cap) and pair[0].supports(capability)
        ]
        self._candidate_cache[cache_key] = (self._generation, supported)
        return supported

    def _capability_cooled(self, info: ModelInfo, *capabilities: ModelCapability) -> bool:
        """True when *info* is under a capability cooldown for any of *capabilities*."""
        key: _CooldownKey = (info.model_id, info.provider_name)
        return any(self._cooldowns.is_capability_active(key, str(cap)) for cap in capabilities)

    def _candidates(
        self,
        capability: ModelCapability,
        priority: PoolPriority,
        estimated_tokens: int,
        pool: str | None = None,
    ) -> list[_Candidate]:
        target_pool = (pool or PRIORITY_TO_POOL.get(priority, "speed")).lower()
        if target_pool == "reasoning":
            req_cap = ModelCapability.REASONING
        elif target_pool in ("speed", "fast_cheap"):
            req_cap = ModelCapability.SPEED
        elif target_pool == "vision":
            req_cap = ModelCapability.VISION
        elif target_pool in ("transcription", "audio"):
            req_cap = capability
        elif target_pool in ("embeddings", "embedding"):
            req_cap = ModelCapability.EMBEDDING
        else:
            req_cap = capability

        # Which models *can* serve this (req_cap, capability) pair is a pure function
        # of the registry, so it is cached per registry generation; every dynamic
        # check (capability cooldowns, rate limits, provider health) still runs below
        # on each call, so a cooling-down model is never served from cache.
        supports_both = self._capability_supported(req_cap, capability)
        capable = [pair for pair in supports_both if not self._capability_cooled(pair[0], req_cap, capability)]
        if not capable:
            supports_base = self._capability_supported(capability, capability)
            capable = [pair for pair in supports_base if not self._capability_cooled(pair[0], capability)]
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
            estimated_payload_chars = (estimated_tokens * 4) + _SYSTEM_PROMPT_CHARS
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

        if not available:
            # Fallback to any healthy model supporting the base capability that fits payload
            estimated_payload_chars = (estimated_tokens * 4) + _SYSTEM_PROMPT_CHARS if estimated_tokens > 0 else 0
            available = [
                (info, provider)
                for info, provider in self._registry.values()
                if info.supports(capability)
                and not self._cooldowns.is_capability_active((info.model_id, info.provider_name), str(capability))
                and (info.context_window == 0 or estimated_tokens <= 0 or info.context_window >= estimated_tokens)
                and (
                    info.max_payload_chars is None
                    or estimated_payload_chars <= 0
                    or estimated_payload_chars <= info.max_payload_chars
                )
                and not self._cooldowns.is_active((info.model_id, info.provider_name))
                and self._provider_health.is_available(info.provider_name)
            ]

        # Precompute quality scores once to avoid repeated EMA calculations during sort/shuffle.
        quality: dict[_CooldownKey, float] = {
            (info.model_id, info.provider_name): self._effective_quality(info) for info, _ in available
        }

        if target_pool == "reasoning" or priority == PoolPriority.HIGH:
            available.sort(key=lambda x: quality[(x[0].model_id, x[0].provider_name)], reverse=True)
            available = shuffle_groups(available, key=lambda x: round(quality[(x[0].model_id, x[0].provider_name)], 6))
        elif priority == PoolPriority.LOW or target_pool == "high_volume":
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
        else:  # SPEED / MEDIUM: free models first, shuffle within each free/paid tier.
            available.sort(key=lambda x: (not x[0].is_free, -quality[(x[0].model_id, x[0].provider_name)]))
            available = shuffle_groups(
                available,
                key=lambda x: (not x[0].is_free, round(quality[(x[0].model_id, x[0].provider_name)], 6)),
            )

        return self._promote_preferred(available, capability, priority)

    def _free_fallback_candidates(self, capability: ModelCapability, estimated_tokens: int) -> list[_Candidate]:
        """All free, healthy, context-fitting models - the safety net.

        Used when a priority pool (e.g. reasoning/fast_cheap) is entirely
        unavailable (out of credits, rate-limited, down). Returns the same kind of
        candidate list as ``_candidates`` but drawn from free models only, so a
        request can still complete on the free tier instead of failing with
        "No models available". Excludes models in cooldown or behind a dead
        provider breaker, and respects the payload-size cap.
        """
        capable = [
            pair
            for pair in self._registry.values()
            if pair[0].supports(capability)
            and not self._cooldowns.is_capability_active((pair[0].model_id, pair[0].provider_name), str(capability))
        ]
        if estimated_tokens > 0:
            capable = [
                (info, provider)
                for info, provider in capable
                if info.context_window == 0 or info.context_window >= estimated_tokens
            ]
        estimated_payload_chars = (estimated_tokens * 4) + _SYSTEM_PROMPT_CHARS if estimated_tokens > 0 else 0
        free: list[_Candidate] = [
            (info, provider)
            for info, provider in capable
            if info.is_free
            and not self._cooldowns.is_active((info.model_id, info.provider_name))
            and self._provider_health.is_available(info.provider_name)
            and (
                info.max_payload_chars is None
                or estimated_payload_chars <= 0
                or estimated_payload_chars <= info.max_payload_chars
            )
        ]
        if not free:
            return []
        # Free-first ranking (best quality first within the free tier).
        quality: dict[_CooldownKey, float] = {
            (info.model_id, info.provider_name): self._effective_quality(info) for info, _ in free
        }
        free.sort(key=lambda x: (not x[0].is_free, -quality[(x[0].model_id, x[0].provider_name)]))
        return self._promote_preferred(
            shuffle_groups(
                free,
                key=lambda x: (not x[0].is_free, round(quality[(x[0].model_id, x[0].provider_name)], 6)),
            ),
            capability,
            PoolPriority.LOW,
        )

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
        fallback_candidates: list[_Candidate] | _Deferred | None = None,
        allow_wait: bool = True,
        capability: ModelCapability | str | None = None,
    ):
        # Resolved only where it is used, so a deferred fallback stays unbuilt on
        # the common path where the primary chain succeeds.
        def _fallback() -> list[_Candidate]:
            if fallback_candidates is None:
                return []
            return fallback_candidates.get() if isinstance(fallback_candidates, _Deferred) else fallback_candidates

        if not candidates:
            # Nothing in the primary pool - go straight to the fallback (free tier)
            # if one was supplied, otherwise fail fast.
            resolved = _fallback()
            if resolved:
                candidates = resolved
                used_fallback = True
            else:
                raise AllModelsRateLimitedError("No models available for this request")
        else:
            used_fallback = False

        if sticky_key is not None:
            candidates = self._apply_stickiness(sticky_key, candidates)

        for info, provider in candidates:
            key: _CooldownKey = (info.model_id, info.provider_name)
            if self._cooldowns.is_active(key):
                continue
            if capability is not None and self._cooldowns.is_capability_active(key, str(capability)):
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
                    if capability is not None:
                        self._cooldowns.set_capability_cooldown(key, str(capability))
                        logger.warning(
                            "Empty/invalid %s response from %s/%s - suspending %s capability for 1h",
                            capability,
                            info.provider_name,
                            info.model_id,
                            capability,
                        )
                    else:
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
            except ModelDegenerateError as e:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                if capability is not None:
                    self._cooldowns.set_capability_cooldown(key, str(capability))
                    logger.warning(
                        "Degenerate %s response from %s/%s (%s) - suspending %s capability for 1h",
                        capability,
                        info.provider_name,
                        info.model_id,
                        e.reason,
                        capability,
                    )
                else:
                    self._cooldowns.set_rate_limit(key, 120)
                    logger.warning(
                        "Degenerate response from %s/%s (%s) - trying next candidate",
                        info.provider_name,
                        info.model_id,
                        e.reason,
                    )
                continue
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
                self._rate_limit_status.record_provider_down(info.provider_name, "provider auth failed")
                logger.warning(
                    "Provider auth failed: %s/%s - skipping provider for 24 h",
                    info.provider_name,
                    info.model_id,
                )
            except ProviderUnavailableError as e:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                state = self._provider_health.mark_degraded(info.provider_name, str(e) or "server outage")
                self._rate_limit_status.record_provider_down(info.provider_name, str(e) or "server outage")
                logger.warning(
                    "Provider degraded: %s - circuit %s (%s)",
                    info.provider_name,
                    state,
                    e,
                )
            except ModelNotFoundError:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                self._cooldowns.set_payment_exhausted(key)
                self._rate_limit_status.record_error(
                    info.provider_name,
                    info.model_id,
                    reason="model not found (404)",
                    is_free=info.is_free,
                )
                logger.info(
                    "Model not found: %s/%s - skipping for 24 h",
                    info.provider_name,
                    info.model_id,
                )
            except InferenceError as e:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                self._rate_limit_status.record_error(
                    info.provider_name,
                    info.model_id,
                    reason=str(e)[:160] or "inference error",
                    is_free=info.is_free,
                )
                logger.warning(
                    "Inference error on %s/%s - trying next candidate: %s",
                    info.provider_name,
                    info.model_id,
                    e,
                )
            except Exception:
                self._record_model_outcome(key, False)
                self._persist_model_score(key)
                raise

        # Primary pool exhausted (all paid models out of credits / rate-limited /
        # down). If a free-tier fallback was supplied, try it before giving up.
        # This is the first point that needs the fallback, so it is built here.
        resolved_fallback = _fallback() if not used_fallback else []
        if resolved_fallback:
            logger.info("Primary pool exhausted - falling back to free-tier models")
            return await self._dispatch(
                resolved_fallback,
                call_fn,
                is_valid=is_valid,
                sticky_key=sticky_key,
                fallback_candidates=None,
                allow_wait=allow_wait,
                capability=capability,
            )

        # Check for transient rate limit cooldowns across all evaluated candidates
        all_evaluated = candidates + resolved_fallback
        transient_waits: list[float] = []
        for info, _ in all_evaluated:
            key = (info.model_id, info.provider_name)
            if not self._cooldowns.is_payment_required(info.provider_name, info.model_id):
                rem = self._cooldowns.remaining(key)
                if rem > 0:
                    transient_waits.append(rem)

        min_wait = min(transient_waits) if transient_waits else 0.0

        if allow_wait and 0 < min_wait <= 40.0:
            logger.info(
                "All candidates transiently rate-limited - pausing in-flight for %.1fs before retrying",
                min_wait,
            )
            await asyncio.sleep(min_wait + 0.5)
            return await self._dispatch(
                candidates,
                call_fn,
                is_valid=is_valid,
                sticky_key=sticky_key,
                fallback_candidates=fallback_candidates,
                allow_wait=False,
                capability=capability,
            )

        raise AllModelsRateLimitedError(
            f"All {len(candidates)} candidate(s) exhausted - every model is rate-limited or has insufficient credits",
            retry_after=min_wait if min_wait > 0 else None,
        )
