"""Selecting a model for one part of a task, and running the call.

This is the whole selection path in one place: take the part's chain, narrow it
to what this call needs, walk it honouring the scope of every failure, and record
what happened. Model choice never consults a name, a tier table or a pool.

It deliberately owns none of the plumbing around it - providers, cost and the
per-model success EMA stay with the dispatcher, which passes what this needs in.
That keeps "which model, and why" a thing you can read in one file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from inference.decisions import EXHAUSTED, FAILED, DecisionLog, RoutingDecision
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    PinnedModelUnavailableError,
)
from inference.facts.catalog import FactsCatalog
from inference.facts.identity import canonical
from inference.facts.models import Entitlement
from inference.failure import OutageCorroboration, Scope, classify, invalid_response
from inference.model_policy import model_matches
from inference.routing.availability import AvailabilityView
from inference.routing.chain import Candidate, ChainWalk, Requirements, context_of, narrow
from inference.routing.parts import PartProfile, profile_for, with_pool, with_power

logger = logging.getLogger(__name__)

# Called with (model_id, provider_name, succeeded) after every attempt, so the
# dispatcher can keep its per-model success EMA without this module knowing it exists.
OutcomeSink = Callable[[str, str, bool], None]
# (model_id, provider, seconds, tokens_out) - one observed generation rate.
LatencySink = Callable[[str, str, float, int], None]
ProviderLookup = Callable[[str], Any]

# A walk that ends with every candidate merely cooling down is waited out and
# retried once, rather than failing a call that would have worked a second later.
# Above this, the wait is the caller's to decide on and the error carries how long.
_MAX_INLINE_WAIT_SECONDS = 40.0
# Cooldowns expire on a clock this process does not own, so the retry sleeps a
# little past the deadline rather than racing it.
_WAIT_GRACE_SECONDS = 0.5

# Distinguishes "the walk produced no answer" from a call that legitimately
# returned None.
_NO_RESULT = object()


class ChainRouter:
    """Routes one call by walking the chain for its part."""

    def __init__(
        self,
        catalog: FactsCatalog,
        decisions: DecisionLog,
        availability: AvailabilityView,
        provider_lookup: ProviderLookup,
        *,
        profiles: dict[str, PartProfile] | None = None,
        corroboration: OutageCorroboration | None = None,
        on_outcome: OutcomeSink | None = None,
        on_latency: LatencySink | None = None,
        demoted: Callable[[str], bool] | None = None,
        slow: Callable[[str], bool] | None = None,
        power: Callable[[], str | None] | None = None,
        pinned: Callable[[], str] | None = None,
    ) -> None:
        self._catalog = catalog
        self._decisions = decisions
        self._availability = availability
        self._provider_lookup = provider_lookup
        self._profiles = profiles or {}
        self._corroboration = corroboration or OutageCorroboration()
        self._on_outcome = on_outcome
        self._on_latency = on_latency
        self._demoted = demoted
        self._slow = slow
        # Manual routing: the one model the user chose, read live so switching
        # back to auto takes effect on the next call rather than the next restart.
        self._pinned = pinned
        self._power = power

    @property
    def catalog(self) -> FactsCatalog:
        return self._catalog

    def set_profiles(self, profiles: dict[str, PartProfile]) -> None:
        self._profiles = profiles

    def _is_wired(self, provider: str) -> bool:
        """True when *provider* is currently configured on this install."""
        return self._provider_lookup(provider) is not None

    @property
    def is_ready(self) -> bool:
        """False until a catalog exists, so the caller can keep the legacy path."""
        return not self._catalog.snapshot.is_empty

    # ---- selection ----

    def chain_for(
        self, component: str, requirements: Requirements, pool: str | None = None
    ) -> tuple[list[Candidate], PartProfile]:
        """The chain for *component*, narrowed to what this call needs.

        Raises :class:`ContextTooLargeError` when models qualify on capability but
        none has a window big enough, so the agent layer can compact and retry
        rather than being told, unhelpfully, that nothing is available. A prompt
        that only trips a provider's *payload* cap is a different problem with a
        different fix, and is never reported as a context overflow.
        """
        profile = profile_for(component, self._profiles)
        profile = with_pool(profile, pool)
        profile = with_power(profile, self._power() if self._power else None)
        full = self._only_pinned(self._catalog.chain_for(profile, self._demoted))
        eligible = narrow(
            full,
            Requirements(capabilities=requirements.capabilities, exclude=requirements.exclude),
        )
        if not eligible and requirements.exclude:
            # Excluding left nothing at all - the excluded model is the only one
            # that qualifies. Proceed without the exclusion rather than blocking
            # the task on model scarcity; the decision log records that it
            # happened, so a non-independent review is never a silent one.
            logger.warning(
                "Excluding %s left no candidates for %s - proceeding without the exclusion",
                sorted(requirements.exclude),
                profile.part,
            )
            # Dropped from `requirements` too, not just from this step: the final
            # narrow below applies them in full, and re-applying the exclusion
            # there emptied the chain again - so the call failed anyway, having
            # logged that it would not.
            requirements = replace(requirements, exclude=frozenset())
            eligible = narrow(full, Requirements(capabilities=requirements.capabilities))

        # Context and payload are separated deliberately. Compaction is the fix
        # for one and useless for the other, so conflating them had the agent
        # layer shorten its history and retry into the same wall.
        fits_context = narrow(
            eligible,
            Requirements(capabilities=requirements.capabilities, min_context=requirements.min_context),
        )
        if not fits_context and eligible and requirements.min_context:
            largest = max((context_of(c.facts, c.endpoints) or 0) for c in eligible)
            raise ContextTooLargeError(requirements.min_context, largest)
        return self._quickest_first(narrow(fits_context, requirements)), profile

    def _only_pinned(self, chain: list[Candidate]) -> list[Candidate]:
        """Under manual routing, reduce the chain to the one model that was named.

        A *hard* pin, unlike ``PartProfile.pinned_model``, which moves a model to
        the head and keeps the rest behind it. Falling back is right when north is
        choosing and wrong when the user has: the point of naming a model is that
        the answer came from that model, so a pin that cannot be honoured is an
        error rather than a preference north quietly declines.

        Requirements still apply afterwards. A pinned model too small for the part
        fails as a context overflow, which is the true reason, rather than being
        reported as an unavailable pin.
        """
        pin = (self._pinned() if self._pinned else "") or ""
        if not pin:
            return chain
        kept = [
            candidate
            for candidate in chain
            if any(model_matches(pin, e.provider, e.provider_model_id) for e in candidate.endpoints)
        ]
        if not kept:
            raise PinnedModelUnavailableError(
                f"Manual routing is pinned to {pin!r}, which nothing in the model catalog matches. "
                "Pick another model, or switch routing back to auto."
            )
        return kept

    def _quickest_first(self, candidates: list[Candidate]) -> list[Candidate]:
        """Move models this install has measured as slow to the tail, order intact.

        Applied here rather than in ``build_chain`` because the catalog caches one
        chain per (profile, generation): folding speed in there would freeze the
        ordering at build time, and a model that turns slow an hour later would
        keep its place until the next catalog refresh. Ranking, never filtering -
        a slow model is still the right answer when it is the only one that
        qualifies.
        """
        if self._slow is None or len(candidates) < 2:
            return candidates
        quick = [c for c in candidates if not self._slow(c.canonical_id)]
        return quick + [c for c in candidates if self._slow(c.canonical_id)] if quick else candidates

    # ---- dispatch ----

    async def dispatch(
        self,
        *,
        component: str,
        requirements: Requirements,
        call_fn: Callable[[Any, str], Awaitable[Any]],
        is_valid: Callable[[Any], bool] | None = None,
        capability: str | None = None,
        task_id: str | None = None,
        pool: str | None = None,
    ) -> Any:
        """Walk the chain for *component* until one endpoint returns a usable answer.

        A walk where nothing was left but cooling-down endpoints is waited out and
        walked again, once, when the soonest of those cooldowns is short: a model
        rate-limited for a tenth of a second is not a reason to fail a call. The
        chain is rebuilt after the wait, because what is available has changed.
        """
        result, walk, profile = await self._walk(
            component, requirements, call_fn, is_valid, capability, task_id, pool
        )
        if result is not _NO_RESULT:
            return result

        wait = walk.soonest_retry()
        if wait is not None and 0 < wait <= _MAX_INLINE_WAIT_SECONDS:
            logger.info("Every candidate is cooling down - waiting %.1fs and walking again", wait)
            await asyncio.sleep(wait + _WAIT_GRACE_SECONDS)
            result, walk, profile = await self._walk(
                component, requirements, call_fn, is_valid, capability, task_id, pool
            )
            if result is not _NO_RESULT:
                return result

        raise AllModelsRateLimitedError(
            f"No model could serve {profile.part} - {walk.exhaustion_summary(requirements)}",
            retry_after=walk.soonest_retry(),
        )

    async def _walk(
        self,
        component: str,
        requirements: Requirements,
        call_fn: Callable[[Any, str], Awaitable[Any]],
        is_valid: Callable[[Any], bool] | None,
        capability: str | None,
        task_id: str | None,
        pool: str | None,
    ) -> tuple[Any, ChainWalk, PartProfile]:
        """One pass down the chain. Returns ``_NO_RESULT`` when it is exhausted."""
        chain, profile = self.chain_for(component, requirements, pool)
        decision = RoutingDecision(
            part=profile.part,
            task_id=task_id,
            requirements=_describe(requirements, capability),
        )
        walk = ChainWalk(chain, self._availability, capability, self._is_wired)

        for attempt in walk.attempts():
            provider = self._provider_lookup(attempt.provider)
            if provider is None:  # pragma: no cover - the walk filters these out
                continue
            started = time.monotonic()
            try:
                result = await call_fn(provider, attempt.model_id)
            except Exception as exc:
                failure = classify(
                    exc,
                    model_id=attempt.model_id,
                    provider=attempt.provider,
                    capability=capability,
                    is_free=attempt.endpoint.is_free,
                )
                attempt.failed(failure)
                if failure.scope is Scope.REQUEST:
                    # The caller's problem, not the model's: nothing is cooled and
                    # walking on would only repeat it.
                    decision.outcome = FAILED
                    self._decisions.record(_finish(decision, walk))
                    raise
                self._apply(failure, attempt)
                continue

            if is_valid is not None and not is_valid(result):
                failure = invalid_response(attempt.model_id, attempt.provider, capability)
                self._apply(failure, attempt)
                attempt.failed(failure)
                continue

            self._record_speed(attempt, time.monotonic() - started, result)
            self._succeed(attempt)
            decision.chose(attempt.model_id, attempt.provider)
            self._decisions.record(_finish(decision, walk))
            return result, walk, profile

        decision.outcome = EXHAUSTED
        self._decisions.record(_finish(decision, walk))
        return _NO_RESULT, walk, profile

    # ---- outcome handling ----

    def _record_speed(self, attempt, seconds: float, result: Any) -> None:
        """Report how fast this endpoint actually generated, for later ranking.

        Only successful calls are timed. A call that failed says nothing about how
        quickly the model produces text, and a timeout would otherwise be recorded
        as the slowest result of all and tail a model for being unavailable rather
        than for being slow - which is what cooldowns are already for.
        """
        if self._on_latency is None:
            return
        tokens_out = getattr(result, "tokens_out", 0) or 0
        self._on_latency(attempt.model_id, attempt.provider, seconds, int(tokens_out))

    def _succeed(self, attempt) -> None:
        # Only persist entitlement when it actually changed. A paid call going
        # through is news exactly once - after that, rewriting every endpoint row
        # for the provider on every successful call would put a table update on
        # the inference path for no new information.
        was_restricted = self._availability.entitlements.entitlement_of(attempt.endpoint) is not Entitlement.UNKNOWN
        self._availability.record_success(attempt.endpoint)
        self._corroboration.clear(attempt.provider)
        if was_restricted:
            # Only the tier that just answered is cleared. A free model working
            # says nothing about whether the paid tier has been funded.
            self._catalog.entitlement_updates(
                attempt.provider, Entitlement.OK, tier="free" if attempt.endpoint.is_free else "paid"
            )
        if self._on_outcome is not None:
            self._on_outcome(attempt.model_id, attempt.provider, True)

    # Which fact a capability failure contradicts. A capability north cannot map
    # onto a declared fact still gets its cooldown; it just has nothing to demote.
    _CAPABILITY_FIELDS: dict[str, str] = {
        "tool_calls": "supports_tools",
        "structured_output": "supports_structured",
        "reasoning": "supports_reasoning",
        "completion": "supports_completion",
    }

    def _apply(self, failure, attempt) -> None:
        """Act on one failure at its own scope, and no wider."""
        provider_down = False
        if failure.scope is Scope.MODEL_CAPABILITY and failure.capability:
            # The model was declared able to do this and could not. Observation
            # contradicts a declaration, but only with corroboration, so this
            # counts a witness and demotes the fact once enough agree.
            field = self._CAPABILITY_FIELDS.get(failure.capability)
            if field is not None:
                self._catalog.contradict(attempt.candidate.canonical_id, field, attempt.provider)
        if failure.scope is Scope.PROVIDER_DOWN:
            provider_down = self._corroboration.record(attempt.provider, attempt.model_id)
            if not provider_down:
                logger.info(
                    "Provider-level failure on %s/%s (%d of %d votes) - not enough to declare it down",
                    attempt.provider,
                    attempt.model_id,
                    self._corroboration.votes(attempt.provider),
                    self._corroboration.quorum,
                )
        self._availability.apply(failure, attempt.endpoint, provider_down=provider_down)
        if failure.scope is Scope.ACCOUNT_PAID:
            self._catalog.entitlement_updates(attempt.provider, Entitlement.NEEDS_BILLING, tier="paid")
        elif failure.scope is Scope.ACCOUNT_FREE:
            self._catalog.entitlement_updates(attempt.provider, Entitlement.FREE_SPENT, tier="free")
        elif failure.scope is Scope.PROVIDER_AUTH:
            self._catalog.entitlement_updates(attempt.provider, Entitlement.FORBIDDEN, tier="any")
        if self._on_outcome is not None and failure.scope is not Scope.REQUEST:
            self._on_outcome(attempt.model_id, attempt.provider, False)


def requirements_from(
    *,
    estimated_tokens: int = 0,
    payload_chars: int = 0,
    needs_tools: bool = False,
    needs_structured: bool = False,
    needs_vision: bool = False,
    exclude_models: list[str] | None = None,
) -> Requirements:
    """Derive this call's own requirements from the request that was made.

    Nothing here is configured: a call carrying tools needs tool support, a
    prompt of N tokens needs an N-token window, ``json_mode`` needs structured
    output. A brand-new call site is routed sensibly without naming anything.
    """
    from inference.routing.parts import STRUCTURED, TOOLS, VISION

    capabilities: set[str] = set()
    if needs_tools:
        capabilities.add(TOOLS)
    if needs_structured:
        capabilities.add(STRUCTURED)
    if needs_vision:
        capabilities.add(VISION)
    excluded = {canonical(m) for m in (exclude_models or []) if m and m.strip()}
    return Requirements(
        capabilities=frozenset(capabilities),
        min_context=max(0, estimated_tokens),
        max_payload_chars=max(0, payload_chars),
        exclude=frozenset(excluded),
    )


def _describe(requirements: Requirements, capability: str | None) -> dict[str, object]:
    described: dict[str, object] = dict.fromkeys(sorted(requirements.capabilities), True)
    if requirements.min_context:
        described["context_window"] = requirements.min_context
    if requirements.exclude:
        described["excludes"] = sorted(requirements.exclude)
    if capability:
        described["capability"] = capability
    return described


def _finish(decision: RoutingDecision, walk: ChainWalk) -> RoutingDecision:
    decision.considered = walk.considered
    decision.skipped = [skip.as_dict() for skip in walk.skipped]
    # The stored list is capped, so the true total travels separately - otherwise
    # a reader counts 40 endpoints and concludes that is all north tried.
    decision.endpoints = len(walk.skipped)
    decision.attempted = sum(1 for skip in walk.skipped if skip.tried)
    return decision
