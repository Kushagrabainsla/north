"""Selecting a model for one part of a task, and running the call.

This is the whole selection path in one place: take the part's chain, narrow it
to what this call needs, walk it honouring the scope of every failure, and record
what happened. Model choice never consults a name, a tier table or a pool.

It deliberately owns none of the plumbing around it - providers, cost, the
per-model success EMA and the legacy pool path stay with the dispatcher, which
passes what this needs in. That keeps "which model, and why" a thing you can
read in one file.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from inference.decisions import EXHAUSTED, FAILED, DecisionLog, RoutingDecision
from inference.exceptions import AllModelsRateLimitedError, ContextTooLargeError
from inference.facts.catalog import FactsCatalog
from inference.facts.identity import canonical
from inference.facts.models import Entitlement
from inference.failure import OutageCorroboration, Scope, classify, invalid_response
from inference.routing.availability import AvailabilityView
from inference.routing.chain import Candidate, ChainWalk, Requirements, context_of, narrow
from inference.routing.parts import PartProfile, profile_for, with_power

logger = logging.getLogger(__name__)

# Called with (model_id, provider_name, succeeded) after every attempt, so the
# dispatcher can keep its per-model success EMA without this module knowing it exists.
OutcomeSink = Callable[[str, str, bool], None]
ProviderLookup = Callable[[str], Any]


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
        demoted: Callable[[str], bool] | None = None,
        power: Callable[[], str | None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._decisions = decisions
        self._availability = availability
        self._provider_lookup = provider_lookup
        self._profiles = profiles or {}
        self._corroboration = corroboration or OutageCorroboration()
        self._on_outcome = on_outcome
        self._demoted = demoted
        self._power = power

    @property
    def catalog(self) -> FactsCatalog:
        return self._catalog

    def set_profiles(self, profiles: dict[str, PartProfile]) -> None:
        self._profiles = profiles

    @property
    def is_ready(self) -> bool:
        """False until a catalog exists, so the caller can keep the legacy path."""
        return not self._catalog.snapshot.is_empty

    # ---- selection ----

    def chain_for(self, component: str, requirements: Requirements) -> tuple[list[Candidate], PartProfile]:
        """The chain for *component*, narrowed to what this call needs.

        Raises :class:`ContextTooLargeError` when models qualify on capability but
        none has a window big enough, so the agent layer can compact and retry
        rather than being told, unhelpfully, that nothing is available.
        """
        profile = with_power(profile_for(component, self._profiles), self._power() if self._power else None)
        full = self._catalog.chain_for(profile, self._demoted)
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
            eligible = narrow(full, Requirements(capabilities=requirements.capabilities))
        fitting = narrow(eligible, requirements)
        if not fitting and eligible and requirements.min_context:
            largest = max((context_of(c.facts, c.endpoints) or 0) for c in eligible)
            raise ContextTooLargeError(requirements.min_context, largest)
        return fitting, profile

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
    ) -> Any:
        """Walk the chain for *component* until one endpoint returns a usable answer."""
        chain, profile = self.chain_for(component, requirements)
        decision = RoutingDecision(
            part=profile.part,
            task_id=task_id,
            requirements=_describe(requirements, capability),
        )
        walk = ChainWalk(chain, self._availability, capability)

        for attempt in walk.attempts():
            provider = self._provider_lookup(attempt.provider)
            if provider is None:
                # The catalog lists an endpoint whose provider is no longer wired
                # up (a key was removed since the last refresh). Skip it quietly.
                attempt.failed(invalid_response(attempt.model_id, attempt.provider, None))
                continue
            try:
                result = await call_fn(provider, attempt.model_id)
            except Exception as exc:
                failure = classify(
                    exc, model_id=attempt.model_id, provider=attempt.provider, capability=capability
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

            self._succeed(attempt)
            decision.chose(attempt.model_id, attempt.provider)
            self._decisions.record(_finish(decision, walk))
            return result

        decision.outcome = EXHAUSTED
        self._decisions.record(_finish(decision, walk))
        raise AllModelsRateLimitedError(
            f"No model could serve {profile.part} - {walk.exhaustion_summary()}",
            retry_after=_soonest_retry(walk),
        )

    # ---- outcome handling ----

    def _succeed(self, attempt) -> None:
        # Only persist entitlement when it actually changed. A paid call going
        # through is news exactly once - after that, rewriting every endpoint row
        # for the provider on every successful call would put a table update on
        # the inference path for no new information.
        was_restricted = (
            not attempt.endpoint.is_free
            and self._availability.entitlements.entitlement_of(attempt.endpoint) is not Entitlement.UNKNOWN
        )
        self._availability.record_success(attempt.endpoint)
        self._corroboration.clear(attempt.provider)
        if was_restricted:
            self._catalog.entitlement_updates(attempt.provider, Entitlement.OK, paid_only=True)
        if self._on_outcome is not None:
            self._on_outcome(attempt.model_id, attempt.provider, True)

    def _apply(self, failure, attempt) -> None:
        """Act on one failure at its own scope, and no wider."""
        provider_down = False
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
            self._catalog.entitlement_updates(attempt.provider, Entitlement.NEEDS_BILLING, paid_only=True)
        elif failure.scope is Scope.PROVIDER_AUTH:
            self._catalog.entitlement_updates(attempt.provider, Entitlement.FORBIDDEN, paid_only=False)
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
    return decision


def _soonest_retry(walk: ChainWalk) -> float | None:
    """The shortest wait any skipped candidate named, so callers can back off usefully."""
    waits = [
        float(reason.split("(")[1].rstrip("s)"))
        for skip in walk.skipped
        if (reason := skip.reason).startswith("cooling down (")
    ]
    return min(waits) if waits else None
