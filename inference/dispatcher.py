"""ModelDispatcher - what the rest of north calls to reach a model.

Implements InferenceRouter over an ordered list of Provider instances. It no
longer decides *which* model answers a completion: that is the chain router's
job (``inference/routing/``), which ranks the whole catalog per part of a task
from fetched facts. What lives here is everything around that decision -

  * the provider registry, and the catalog refresh that keeps it current
  * cooldowns, provider health, and precise rate-limit status
  * the per-model success and generation-rate EMAs this install has measured
  * embeddings and transcription, which are selected here rather than by the
    chain: no catalog source declares either capability, so the provider's own
    flags are the only place that knowledge exists

Context overflow: raises ContextTooLargeError so the agent layer can compact
the conversation and retry. See agents/context_compaction.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config.strategy import NorthSettings
from inference.base import InferenceRouter
from inference.capability import ModelCapability, ModelInfo
from inference.constants import (
    _DEFAULT_MODEL_CONFIDENCE,
    _MODEL_CONFIDENCE_ALPHA,
    _MODEL_SPEED_ALPHA,
    _MODEL_SPEED_FLOOR_TOK_PER_SEC,
    _MODEL_SPEED_MIN_SAMPLES,
    _PREFERRED_HEALTH_FLOOR,
    _PREFERRED_MIN_USES,
    _QUALITY_TIER_HIGH,
    _QUALITY_TIER_MEDIUM,
)
from inference.cooldowns import CooldownStore, _CooldownKey
from inference.decisions import DecisionLog
from inference.exceptions import (
    AllModelsRateLimitedError,
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    PayloadTooLargeError,
    PaymentRequiredError,
    ProviderAuthError,
    ProviderUnavailableError,
    RoutingNotReadyError,
)
from inference.facts.catalog import FactsCatalog
from inference.facts.identity import canonical
from inference.facts.store import ModelFactsStore
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelEntry,
    ModelPool,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.provider import Provider
from inference.provider_health import ProviderHealthTracker
from inference.providers.local_embeddings import PROVIDER_NAME as LOCAL_EMBEDDINGS
from inference.rate_limit_status import _PAYLOAD_TOO_LARGE_SECS, RateLimitStatusStore
from inference.routing.availability import AvailabilityView, EntitlementLedger
from inference.routing.parts import parse_profiles
from inference.routing.router import ChainRouter, requirements_from
from utils.text import extract_json

# Allowance (chars) for north's system prompt when estimating total request size
# for the max_payload_chars fit check. Keeps providers with tiny request caps
# (e.g. Groq free) out of candidate selection for large prompts that would 413.
_SYSTEM_PROMPT_CHARS = 8_000

# One model paired with the provider that serves it. Only the infrastructure
# calls - embeddings and transcription - still work in these terms; the chain
# router speaks in endpoints, which carry the facts it ranks on.
_Candidate = tuple[ModelInfo, Provider]

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


def _messages_carry_images(messages: list[dict]) -> bool:
    """True when any message carries image content.

    The agent loop sends screenshots and diagrams as ``image_url`` parts inside
    the tool-calling conversation, not through ``CompletionRequest.images``, so a
    request's need for a vision model has to be read off the messages themselves.
    """
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )


def _toolcall_has_output(resp: Any) -> bool:
    """A tool-call response is usable if it invoked tools, produced text, or reasoning."""
    if getattr(resp, "calls", None):
        return True
    content = getattr(resp, "content", None)
    if bool(content and content.strip()):
        return True
    reasoning = getattr(resp, "reasoning", None)
    return bool(reasoning and reasoning.strip())


# Returned by a candidate call that failed in a way the next candidate can answer.
_NO_RESULT = object()

_DEGENERATE_COOLDOWN_SECS = 120
_MAX_INLINE_WAIT_SECONDS = 40.0
_WAIT_GRACE_SECONDS = 0.5
_ERROR_REASON_CHARS = 160


@dataclass(frozen=True)
class _DispatchPlan:
    """One dispatch: what to call on each candidate, and how to judge the answer."""

    call_fn: Callable[[Provider, str], Awaitable]
    is_valid: Callable[[Any], bool] | None = None
    allow_wait: bool = True
    capability: ModelCapability | str | None = None

class ModelDispatcher(InferenceRouter):
    """Routes inference calls across multiple providers with per-model cooldowns."""

    # Chain routing is assembled in __init__ and is absent until the catalog
    # opens, so these carry class-level defaults rather than being read through
    # getattr at every use site.
    _chain_router: ChainRouter | None = None
    _decisions: DecisionLog | None = None
    _availability: AvailabilityView | None = None

    def __init__(
        self,
        providers: list[Provider],
        north_settings: NorthSettings | None = None,
        confidence_tracker: ConfidenceTracker | None = None,
        cooldowns_path: Path | None = None,
        models_db_path: Path | None = None,
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
        # (model_id, provider_name) → (ema_tokens_per_sec, samples). Same shape and
        # same lifecycle as the reliability EMA above: what this install measured.
        self._model_speed: dict[_CooldownKey, tuple[float, int]] = (
            confidence_tracker.load_model_speeds_sync() if confidence_tracker is not None else {}
        )
        # Scores changed since the last batched DB flush.
        self._dirty_scores: set[_CooldownKey] = set()
        self._flush_task: asyncio.Task | None = None
        # Bumped whenever the registry is rebuilt (a pool refresh or provider swap).
        self._generation: int = 0
        self._context_windows: dict[str, int] = {}
        self._context_windows_normalised: dict[str, int] = {}
        # Provider catalogues are fetched over the network after construction, so
        # until the first refresh_pools() returns an empty registry means "not
        # loaded yet" rather than "no models available".
        self._catalog_refreshed: bool = False
        self._build_registry()
        self._cooldowns.load()
        self._rate_limit_status.load()
        self._chain_router = self._build_chain_router(models_db_path, cooldowns_path)

    def _build_chain_router(self, models_db_path: Path | None, cooldowns_path: Path | None) -> ChainRouter | None:
        """Assemble the facts-driven router, or None when there is no catalog to read.

        Returns None rather than raising: a dispatcher is constructed at startup,
        before anything is routed, and a missing or unreadable models.db must not
        take the process down. Calls made while it is None fail one at a time,
        with a message that says so - see ``_chain``.
        """
        db_path = models_db_path or (cooldowns_path.parent / "models.db" if cooldowns_path is not None else None)
        if db_path is None:
            # No path was given, which is how this dispatcher says "do not persist"
            # - CooldownStore(None) is already memory-only on the same signal.
            # Reaching for ~/.north here instead meant a dispatcher constructed in
            # a test or a script silently read and wrote the user's real catalog,
            # and behaved differently depending on what was in it.
            logger.debug("Model routing: no models.db path given - routing is unavailable")
            return None
        try:
            catalog = FactsCatalog(ModelFactsStore(db_path))
            decisions = DecisionLog(db_path)
        except Exception:
            logger.warning("Could not open %s - routing is unavailable", db_path, exc_info=True)
            return None
        self._decisions = decisions
        self._availability = AvailabilityView(
            self._cooldowns, self._provider_health, EntitlementLedger(), self._rate_limit_status
        )
        return ChainRouter(
            catalog,
            decisions,
            self._availability,
            self._provider_by_name,
            profiles=self._routing_profiles(),
            on_outcome=self._record_chain_outcome,
            on_latency=self._record_chain_speed,
            demoted=self._is_demoted,
            slow=self._is_slow,
            power=self._power_mode,
            pinned=self._pinned_model,
        )

    def _power_mode(self) -> str | None:
        """The user's current power setting, read live so a change needs no restart.

        Meaningless under manual routing - a chain of one has nothing to order -
        so it is not even offered there.
        """
        if self._north_settings is None or self._north_settings.pinned_model:
            return None
        return self._north_settings.power.value

    def _pinned_model(self) -> str:
        """The model manual routing pins every call to, or "" when north is choosing."""
        return self._north_settings.pinned_model if self._north_settings is not None else ""

    # ---- chain routing support ----

    def _provider_by_name(self, name: str) -> Provider | None:
        return next((p for p in self._providers if p.name == name), None)

    def _routing_profiles(self) -> dict:
        """Per-part overrides from settings.json, re-read whenever they may have changed."""
        if self._north_settings is None:
            return {}
        return parse_profiles(self._north_settings.routing_parts)

    def _record_chain_outcome(self, model_id: str, provider_name: str, success: bool) -> None:
        key: _CooldownKey = (model_id, provider_name)
        self._record_model_outcome(key, success)
        self._persist_model_score(key)

    def _record_chain_speed(self, model_id: str, provider_name: str, seconds: float, tokens_out: int) -> None:
        """Fold one observed generation rate into this model's speed EMA.

        Rate, not wall-clock: a long answer legitimately takes longer, and ranking
        on raw duration would punish a model for being asked a bigger question.
        Calls that produced no tokens (embeddings, an empty reply) carry no rate
        and are ignored rather than recorded as zero.
        """
        if tokens_out <= 0 or seconds <= 0:
            return
        key: _CooldownKey = (model_id, provider_name)
        rate = tokens_out / seconds
        prev_rate, prev_samples = self._model_speed.get(key, (rate, 0))
        blended = _MODEL_SPEED_ALPHA * rate + (1 - _MODEL_SPEED_ALPHA) * prev_rate
        self._model_speed[key] = (blended, prev_samples + 1)
        self._persist_model_score(key)

    def _is_slow(self, canonical_id: str) -> bool:
        """True when every endpoint this install has timed for the model is slow.

        Mirrors ``_is_demoted``: the model stays in the chain, it just stops being
        preferred. "Slow here" is not "unusable", and a model nobody has timed yet
        is never slow - an untried model must not be tailed before it has run.
        """
        rates = [
            (rate, samples)
            for (model_id, _provider), (rate, samples) in self._model_speed.items()
            if canonical(model_id) == canonical_id
        ]
        if not rates:
            return False
        return all(
            samples >= _MODEL_SPEED_MIN_SAMPLES and rate < _MODEL_SPEED_FLOOR_TOK_PER_SEC
            for rate, samples in rates
        )

    def _is_demoted(self, canonical_id: str) -> bool:
        """True when this install's own history says a model belongs in the tail.

        The signal is the existing per-model success EMA: a model north has tried
        enough times here, and that keeps failing here, is ranked below its
        measured score - but kept in the chain, because "worse on this install" is
        not "unusable".
        """
        scores = [
            (ema, uses)
            for (model_id, _provider), (ema, uses) in self._model_confidence.items()
            if canonical(model_id) == canonical_id
        ]
        if not scores:
            return False
        return all(uses >= _PREFERRED_MIN_USES and ema < _PREFERRED_HEALTH_FLOOR for ema, uses in scores)

    def _chain(self) -> ChainRouter:
        """The router every completion goes through, or a clear failure.

        There is no second router to fall back to since the pool router was
        removed, and that is deliberate: a catalog that has not loaded is
        reported as what it is, rather than quietly served by a different
        selection rule whose answers nobody would be able to account for.
        """
        if self._chain_router is None or not self._chain_router.is_ready:
            raise RoutingNotReadyError(
                "Model routing is not ready - the model catalog has not loaded yet. Retry shortly."
            )
        return self._chain_router

    def routing_decisions(self, *, task_id: str | None = None, part: str | None = None, limit: int = 50) -> list[dict]:
        """Recent routing decisions - "why did the coder run on a free model?"."""
        if self._decisions is None:
            return []
        return self._decisions.recent(task_id=task_id, part=part, limit=limit)

    def entitlement_summary(self) -> dict[str, str]:
        """Per-provider entitlement blocks, for `north limits` and health checks.

        Answers the question a 24-hour provider blackout used to make unanswerable:
        which providers need an action, and which of their models still work.
        """
        if self._availability is None:
            return {}
        return self._availability.entitlements.summary()

    def prune_routing_decisions(self, retention_days: int) -> int:
        """Drop decisions older than *retention_days*. Called by the cleanup job."""
        if self._decisions is None:
            return 0
        return self._decisions.prune(timedelta(days=max(0, retention_days)))

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

        Bumps the generation, and rebuilds the context-window tables that
        `get_context_window` reads on every turn.
        """
        self._generation += 1
        self._context_windows = {}
        self._context_windows_normalised = {}
        # Keyed off info.model_id, not the registry key: every other read path uses
        # .values(), so the value is the authoritative identity here.
        for info, _provider in self._registry.values():
            if info.context_window <= 0:
                continue
            self._context_windows.setdefault(info.model_id, info.context_window)
            self._context_windows_normalised.setdefault(info.model_id.lower().strip(), info.context_window)

    # ---- InferenceRouter ABC ----

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        estimated = len(request.prompt) // 4

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
        return await self._chain().dispatch(
            component=request.component,
            requirements=requirements_from(
                estimated_tokens=estimated,
                payload_chars=len(request.prompt) + _SYSTEM_PROMPT_CHARS,
                needs_structured=request.wants_json,
                needs_vision=bool(request.images),
                exclude_models=request.exclude_models,
            ),
            call_fn=_call,
            is_valid=_valid,
            capability=str(capability),
            task_id=request.task_id,
            pool=request.pool,
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

        return await self._chain().dispatch(
            component=request.component,
            requirements=requirements_from(
                estimated_tokens=estimated,
                payload_chars=(estimated * 4) + _SYSTEM_PROMPT_CHARS,
                needs_tools=bool(request.tools),
                needs_vision=_messages_carry_images(request.messages),
                exclude_models=request.exclude_models,
            ),
            call_fn=_call,
            is_valid=_toolcall_has_output,
            capability=str(ModelCapability.TOOL_CALLS),
            task_id=request.task_id,
            pool=request.pool,
        )

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        candidates = self._prefer_local(self._infrastructure_candidates(ModelCapability.EMBEDDING))

        async def _call(provider: Provider, model_id: str) -> EmbedResponse:
            return await provider.embed(model_id, request)

        return await self._dispatch(
            candidates, _DispatchPlan(call_fn=_call, capability=ModelCapability.EMBEDDING)
        )

    def embedding_model_id(self) -> str:
        """The model embeddings will come from, resolvable before the first call.

        Vector stores stamp themselves with this at construction so they can drop
        vectors from a previous model rather than silently comparing across two
        embedding spaces. That happens at startup, before any catalog refresh, so
        the answer comes from the local provider's own declaration rather than
        from the registry.
        """
        for provider in self._providers:
            model_id = getattr(provider, "model_id", "")
            if provider.name == LOCAL_EMBEDDINGS and model_id:
                return str(model_id)
        for info, _provider in self._registry.values():
            if info.supports(ModelCapability.EMBEDDING):
                return info.model_id
        return ""

    def embedding_provider_name(self) -> str:
        """Who serves embeddings - the on-device model, or a provider over the network.

        Reported alongside the model id so an interface can say *where* the
        vectors are made without inferring it from the shape of a model name.
        """
        for provider in self._providers:
            local_model_id = getattr(provider, "model_id", "")
            if provider.name == LOCAL_EMBEDDINGS and local_model_id:
                return LOCAL_EMBEDDINGS
        for info, _provider in self._registry.values():
            if info.supports(ModelCapability.EMBEDDING):
                return info.provider_name
        return ""

    @staticmethod
    def _prefer_local(candidates: list[_Candidate]) -> list[_Candidate]:
        """Put on-device embeddings first, deterministically.

        Which model produced a vector decides which other vectors it can be
        compared against (see utils/vector_space.py), so this is the one place
        where an arbitrary tie-break between equal-looking candidates would be
        actively harmful: alternating between two embedding models would empty and
        rebuild every index in turn. The local model is always present and costs
        nothing, so it leads and the remote ones stay as the fallback.
        """
        local = [pair for pair in candidates if pair[0].provider_name == LOCAL_EMBEDDINGS]
        return local + [pair for pair in candidates if pair[0].provider_name != LOCAL_EMBEDDINGS]

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        if request.model:
            for (_, mid), (info, provider) in self._registry.items():
                if mid == request.model and info.supports(ModelCapability.TRANSCRIPTION):
                    return await provider.transcribe(mid, request)

        candidates = self._infrastructure_candidates(ModelCapability.TRANSCRIPTION)

        async def _call(provider: Provider, model_id: str) -> TranscriptionResponse:
            return await provider.transcribe(model_id, request)

        return await self._dispatch(
            candidates, _DispatchPlan(call_fn=_call, capability=ModelCapability.TRANSCRIPTION)
        )

    def get_context_window(self, model_id: str) -> int:
        """Return the published context window (tokens) for model_id from the live registry.

        Called once per ReAct turn by context compaction, so exact and normalised
        lookups come from a table built with the registry rather than two linear
        scans over every known model.
        """
        if not model_id:
            return _DEFAULT_CONTEXT_WINDOW

        window = self._facts_context_window(model_id)
        if window:
            return window

        declared = self._context_windows.get(model_id)
        if declared:
            return declared

        norm = model_id.lower().strip()
        declared = self._context_windows_normalised.get(norm)
        if declared:
            return declared

        # Suffix match (e.g. "openai/gpt-4o" against a registry entry "gpt-4o").
        # Rare, so the scan stays here rather than in the indexed path.
        for reg_norm, reg_window in self._context_windows_normalised.items():
            if norm.endswith(reg_norm) or reg_norm.endswith(norm):
                return reg_window

        return _DEFAULT_CONTEXT_WINDOW

    def _facts_context_window(self, model_id: str) -> int:
        """The declared window for *model_id*, from the merged facts.

        Preferred over the registry because facts join across sources on the
        canonical id: a model whose provider publishes nothing still gets its real
        window from whichever source does describe it, instead of a name-table
        guess that compaction then sizes history against.
        """
        if self._chain_router is None:
            return 0
        record = self._chain_router.catalog.snapshot.facts.get(canonical(model_id))
        return int(record.value("context_window") or 0) if record is not None else 0

    async def aclose(self) -> None:
        """Close all provider HTTPX clients. Call on application shutdown."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        await self._flush_dirty_scores()  # don't lose scores batched since the last flush
        if self._decisions is not None:
            await self._decisions.flush()
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
        await self._refresh_facts()

    async def _refresh_facts(self) -> None:
        """Rebuild the fetched-facts catalog and every part's chain.

        Runs on the same cadence as the pool refresh, and reuses the catalogs the
        providers just fetched rather than asking for them again. Never raises:
        a failed refresh keeps the last snapshot, which is the point of persisting
        it.
        """
        if self._chain_router is None:
            return
        self._chain_router.set_profiles(self._routing_profiles())
        await self._chain_router.catalog.refresh(
            self._providers,
            self._models_by_provider(),
            configured_providers=[provider.name for provider in self._providers],
        )

    def _models_by_provider(self) -> dict[str, dict[str, ModelInfo]]:
        """The live registry regrouped as ``{provider: {model_id: info}}``."""
        grouped: dict[str, dict[str, ModelInfo]] = {}
        for info, _provider in self._registry.values():
            grouped.setdefault(info.provider_name, {})[info.model_id] = info
        return grouped

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
        if self._availability is not None:
            # A new key is exactly the action a FORBIDDEN provider was waiting for,
            # so a provider swap clears what the old key proved.
            for provider in providers:
                self._availability.entitlements.clear(provider.name)

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
        items: list[tuple[str, str, float, int, float, int]] = []
        for key in dirty:
            score, uses = self._model_confidence.get(key, (_DEFAULT_MODEL_CONFIDENCE, 0))
            rate, samples = self._model_speed.get(key, (0.0, 0))
            items.append((key[0], key[1], score, uses, rate, samples))
        if items:
            try:
                if hasattr(self._confidence_tracker, "save_model_scores_batch"):
                    await self._confidence_tracker.save_model_scores_batch(items)
                else:
                    for model_id, provider, score, uses, _rate, _samples in items:
                        await self._confidence_tracker.save_model_score(model_id, provider, score, uses)
            except Exception:
                self._dirty_scores.update(dirty)
                logger.warning("Failed to persist model scores batch (%d items)", len(items), exc_info=True)

    # ---- Candidate selection (infrastructure calls) ----

    def _infrastructure_candidates(self, capability: ModelCapability) -> list[_Candidate]:
        """Every model that can serve an embedding or transcription call, cheapest first.

        Completions and tool calls are routed by the chain, from fetched facts.
        These two are not, and they do not want to be: there is typically one
        model for each, the measured scores the chain ranks on say nothing about
        embedding or transcription quality, and *which* model answers an
        embedding call is a correctness question rather than a quality one (see
        ``_prefer_local``). So the rule here is only "can it, and is it up", in a
        deterministic order - never a shuffle.
        """
        available = [
            (info, provider)
            for info, provider in self._registry.values()
            if info.supports(capability) and not self._is_unusable(info, capability)
        ]
        available.sort(key=lambda pair: (pair[0].cost_per_token, pair[0].model_id))
        return available

    # ---- Dispatch ----

    async def _dispatch(self, candidates: list[_Candidate], plan: _DispatchPlan) -> Any:
        """Try each candidate in turn. Used by embeddings and transcription only."""
        if not candidates:
            raise AllModelsRateLimitedError("No models available for this request")

        for info, provider in candidates:
            if self._is_unusable(info, plan.capability):
                continue
            result = await self._call_candidate(info, provider, plan)
            if result is not _NO_RESULT:
                return result

        return await self._all_candidates_failed(candidates, plan)

    def _is_unusable(self, info: ModelInfo, capability: ModelCapability | str | None) -> bool:
        """True when this model is cooling down, or its provider is down."""
        key: _CooldownKey = (info.model_id, info.provider_name)
        if self._cooldowns.is_active(key):
            return True
        if capability is not None and self._cooldowns.is_capability_active(key, str(capability)):
            return True
        return not self._provider_health.is_available(info.provider_name)

    async def _call_candidate(self, info: ModelInfo, provider: Provider, plan: _DispatchPlan) -> Any:
        """Call one model, returning its result or `_NO_RESULT` if the next should be tried."""
        key: _CooldownKey = (info.model_id, info.provider_name)
        try:
            result = await plan.call_fn(provider, info.model_id)
            # A model that returns an empty/degenerate response (200 OK but no
            # usable content) must not count as success - otherwise a single
            # broken model in the pool silently breaks every caller. Treat it
            # like a failure: deprioritise it and fall through to the next.
            if plan.is_valid is not None and not plan.is_valid(result):
                self._penalise_degenerate(info, plan.capability, "empty or invalid response")
                return _NO_RESULT
        except ModelDegenerateError as e:
            self._penalise_degenerate(info, plan.capability, e.reason)
            return _NO_RESULT
        except ModelRateLimitedError as e:
            self._note_rate_limited(info, e)
            return _NO_RESULT
        except PaymentRequiredError:
            self._note_payment_required(info)
            return _NO_RESULT
        except PayloadTooLargeError:
            self._note_payload_too_large(info)
            return _NO_RESULT
        except ProviderAuthError:
            self._note_provider_auth_failed(info)
            return _NO_RESULT
        except ProviderUnavailableError as e:
            self._note_provider_unavailable(info, e)
            return _NO_RESULT
        except ModelNotFoundError:
            self._note_model_not_found(info)
            return _NO_RESULT
        except InferenceError as e:
            self._note_inference_error(info, e)
            return _NO_RESULT
        except Exception:
            self._record_failure(key)
            raise

        self._record_success(info)
        return result

    def _record_failure(self, key: _CooldownKey) -> None:
        self._record_model_outcome(key, False)
        self._persist_model_score(key)

    def _record_success(self, info: ModelInfo) -> None:
        key: _CooldownKey = (info.model_id, info.provider_name)
        self._record_model_outcome(key, True)
        self._persist_model_score(key)
        self._provider_health.record_success(info.provider_name)
        self._rate_limit_status.mark_ok(info.provider_name, info.model_id)

    def _penalise_degenerate(
        self,
        info: ModelInfo,
        capability: ModelCapability | str | None,
        reason: str,
    ) -> None:
        """Deprioritise a model that answered 200 OK with nothing usable in it."""
        key: _CooldownKey = (info.model_id, info.provider_name)
        self._record_failure(key)
        if capability is None:
            self._cooldowns.set_rate_limit(key, _DEGENERATE_COOLDOWN_SECS)
            logger.warning(
                "Degenerate response from %s/%s (%s) - trying next candidate",
                info.provider_name,
                info.model_id,
                reason,
            )
            return
        self._cooldowns.set_capability_cooldown(key, str(capability))
        logger.warning(
            "Degenerate %s response from %s/%s (%s) - suspending %s capability for 1h",
            capability,
            info.provider_name,
            info.model_id,
            reason,
            capability,
        )

    def _note_rate_limited(self, info: ModelInfo, error: ModelRateLimitedError) -> None:
        key: _CooldownKey = (info.model_id, info.provider_name)
        self._cooldowns.set_rate_limit(key, error.retry_after)
        self._rate_limit_status.record_rate_limit(
            info.provider_name,
            info.model_id,
            status_code=error.status_code,
            headers=error.headers,
            body=error.body,
            retry_after=error.retry_after,
            is_free=info.is_free,
        )
        logger.info(
            "Rate limited: %s/%s - skipping for %s",
            info.provider_name,
            info.model_id,
            f"{error.retry_after:.0f}s (Retry-After)" if error.retry_after else "60 s",
        )

    def _note_payment_required(self, info: ModelInfo) -> None:
        self._cooldowns.set_payment_exhausted((info.model_id, info.provider_name))
        self._rate_limit_status.record_payment_required(info.provider_name, info.model_id, is_free=info.is_free)
        logger.warning("Payment required: %s/%s - skipping for 24 h", info.provider_name, info.model_id)

    def _note_payload_too_large(self, info: ModelInfo) -> None:
        # 413: this model can't accept north's request size. Skip it (1h) and
        # route to a model that accepts the payload, rather than retrying.
        self._cooldowns.set_rate_limit((info.model_id, info.provider_name), _PAYLOAD_TOO_LARGE_SECS)
        self._rate_limit_status.record_payload_too_large(info.provider_name, info.model_id, is_free=info.is_free)
        logger.warning("Payload too large: %s/%s - skipping for 1h", info.provider_name, info.model_id)

    def _note_provider_auth_failed(self, info: ModelInfo) -> None:
        self._provider_health.mark_down(info.provider_name, "provider auth failed")
        self._rate_limit_status.record_provider_down(info.provider_name, "provider auth failed")
        logger.warning(
            "Provider auth failed: %s/%s - skipping provider for 24 h",
            info.provider_name,
            info.model_id,
        )

    def _note_provider_unavailable(self, info: ModelInfo, error: ProviderUnavailableError) -> None:
        self._record_failure((info.model_id, info.provider_name))
        outage = str(error) or "server outage"
        state = self._provider_health.mark_degraded(info.provider_name, outage)
        self._rate_limit_status.record_provider_down(info.provider_name, outage)
        logger.warning("Provider degraded: %s - circuit %s (%s)", info.provider_name, state, error)

    def _note_model_not_found(self, info: ModelInfo) -> None:
        key: _CooldownKey = (info.model_id, info.provider_name)
        self._record_failure(key)
        self._cooldowns.set_payment_exhausted(key)
        self._rate_limit_status.record_error(
            info.provider_name,
            info.model_id,
            reason="model not found (404)",
            is_free=info.is_free,
        )
        logger.info("Model not found: %s/%s - skipping for 24 h", info.provider_name, info.model_id)

    def _note_inference_error(self, info: ModelInfo, error: InferenceError) -> None:
        self._record_failure((info.model_id, info.provider_name))
        self._rate_limit_status.record_error(
            info.provider_name,
            info.model_id,
            reason=str(error)[:_ERROR_REASON_CHARS] or "inference error",
            is_free=info.is_free,
        )
        logger.warning(
            "Inference error on %s/%s - trying next candidate: %s",
            info.provider_name,
            info.model_id,
            error,
        )

    async def _all_candidates_failed(self, candidates: list[_Candidate], plan: _DispatchPlan) -> Any:
        """Every candidate is out: wait out a short cooldown, or report exhaustion."""
        wait = self._shortest_transient_wait(candidates)
        if plan.allow_wait and 0 < wait <= _MAX_INLINE_WAIT_SECONDS:
            logger.info(
                "All candidates transiently rate-limited - pausing in-flight for %.1fs before retrying",
                wait,
            )
            await asyncio.sleep(wait + _WAIT_GRACE_SECONDS)
            return await self._dispatch(candidates, replace(plan, allow_wait=False))

        raise AllModelsRateLimitedError(
            f"All {len(candidates)} candidate(s) exhausted - every model is rate-limited or has insufficient credits",
            retry_after=wait if wait > 0 else None,
        )

    def _shortest_transient_wait(self, candidates: list[_Candidate]) -> float:
        """How long until the soonest candidate is out of cooldown; 0.0 if none will be."""
        waits = [
            remaining
            for info, _ in candidates
            if not self._cooldowns.is_payment_required(info.provider_name, info.model_id)
            and (remaining := self._cooldowns.remaining((info.model_id, info.provider_name))) > 0
        ]
        return min(waits) if waits else 0.0
