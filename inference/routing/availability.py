"""Whether an endpoint may be called right now - and, when not, why not.

The old router had five kinds of cooldown, each set from a different exception,
and a provider circuit breaker on top. Nothing carried a reason a person could
read, so exhaustion surfaced as a bare "All N candidates exhausted".

This collapses them into one question - ``skip_reason(endpoint)`` - answered
from four pieces of state, every one of which names itself:

* the provider's key was rejected (needs an action, so no timer)
* the provider is corroborated down (a timed breaker)
* this account cannot use *paid* endpoints on this provider (the free tier is
  untouched, which is the bug this whole layer exists to fix)
* this model, or this model's use of one capability, is cooling down

Reasons are what the decision log records and what ``north limits`` prints, so
they are written to be read by a person, not parsed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from inference.cooldowns import CooldownStore
from inference.facts.models import Endpoint, Entitlement
from inference.failure import Failure, Scope
from inference.provider_health import ProviderHealthTracker
from inference.rate_limit_status import RateLimitStatusStore

# How long "this account needs billing" stands before north tries a paid model
# again. Entitlement is a fact about the account, but accounts get funded, so it
# is a fact with a shelf life rather than a permanent verdict.
BILLING_WINDOW_SECONDS: float = 86_400.0

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entitlement:
    state: Entitlement
    reason: str
    until: float | None  # monotonic; None = no timer, needs an action


class EntitlementLedger:
    """What live calls have proved about this account's access, per provider.

    Deliberately keyed on (provider, paid-or-free): "no payment method" is a fact
    about the account's relationship with one provider's *paid* tier. Applying it
    to the model, or to the whole provider, is what threw away eight working free
    models over one 401.

    Both tiers can be spent, in ways that are not interchangeable. The paid tier
    runs out of money and is fixed by paying. The free tier runs out of allowance
    and is fixed by waiting. Each gets its own entry so neither has to borrow the
    other's story.
    """

    def __init__(self, *, billing_window_seconds: float = BILLING_WINDOW_SECONDS) -> None:
        self._billing_window = billing_window_seconds
        self._paid: dict[str, _Entitlement] = {}
        self._free: dict[str, _Entitlement] = {}
        self._auth: dict[str, _Entitlement] = {}

    def needs_billing(self, provider: str, reason: str) -> None:
        self._paid[provider] = _Entitlement(Entitlement.NEEDS_BILLING, reason, time.monotonic() + self._billing_window)

    def free_spent(self, provider: str, reason: str, *, resets_in: float | None) -> None:
        """This provider's free allowance is used up until it resets.

        The counterpart to :meth:`needs_billing` on the other tier, and the reason
        the free tier needs one at all: without somewhere to put this fact, a
        spent allowance had to be filed as a billing problem, which is both untrue
        and unactionable. ``resets_in`` is the wait the provider itself reported;
        without one the allowance is held for the standard window.
        """
        window = resets_in if resets_in and resets_in > 0 else self._billing_window
        self._free[provider] = _Entitlement(Entitlement.FREE_SPENT, reason, time.monotonic() + window)

    def forbidden(self, provider: str, reason: str) -> None:
        """The key itself was rejected. No timer - a person has to fix it."""
        self._auth[provider] = _Entitlement(Entitlement.FORBIDDEN, reason, None)

    def clear(self, provider: str) -> None:
        """Forget everything believed about this provider - a new key, or a fresh start."""
        self._paid.pop(provider, None)
        self._free.pop(provider, None)
        self._auth.pop(provider, None)

    def clear_tier(self, provider: str, *, is_free: bool) -> None:
        """A call succeeded, so this tier's block was wrong or has expired.

        Only that tier. A free model answering proves the key works and that the
        free allowance is not spent; it proves nothing about whether the paid
        tier has been funded, and clearing that too would send the next call
        straight back into a 402.
        """
        (self._free if is_free else self._paid).pop(provider, None)
        self._auth.pop(provider, None)

    def state_of(self, endpoint: Endpoint) -> _Entitlement | None:
        auth = self._auth.get(endpoint.provider)
        if auth is not None:
            return auth
        tier = self._free if endpoint.is_free else self._paid
        return self._live(tier, endpoint.provider)

    @staticmethod
    def _live(tier: dict[str, _Entitlement], provider: str) -> _Entitlement | None:
        """This tier's entitlement, dropping it once its timer has run out."""
        state = tier.get(provider)
        if state is None:
            return None
        if state.until is not None and state.until <= time.monotonic():
            tier.pop(provider, None)
            return None
        return state

    def entitlement_of(self, endpoint: Endpoint) -> Entitlement:
        state = self.state_of(endpoint)
        return state.state if state is not None else Entitlement.UNKNOWN

    def summary(self) -> dict[str, str]:
        """Per-provider entitlement, for ``north limits`` and the health surface."""
        out = {provider: state.reason for provider, state in self._auth.items()}
        now = time.monotonic()
        for tier in (self._paid, self._free):
            for provider, state in tier.items():
                if state.until is None or state.until > now:
                    out.setdefault(provider, state.reason)
        return out


class AvailabilityView:
    """One place that answers "can this endpoint be called, and if not why not".

    Availability is checked while *walking* the chain, not filtered out when the
    chain is built: a model cooling down when the chain was built may be fine by
    the time the walk reaches it, and every skip carries its reason forward into
    the decision log.
    """

    def __init__(
        self,
        cooldowns: CooldownStore,
        provider_health: ProviderHealthTracker,
        entitlements: EntitlementLedger,
        status: RateLimitStatusStore | None = None,
    ) -> None:
        self._cooldowns = cooldowns
        self._health = provider_health
        self._entitlements = entitlements
        # ``north limits`` reads this. Availability changes are recorded here as
        # they are applied, so what the user is shown is the same state the walk
        # is acting on rather than a parallel account of it.
        self._status = status

    @property
    def entitlements(self) -> EntitlementLedger:
        return self._entitlements

    def skip_reason(self, endpoint: Endpoint, capability: str | None = None) -> str | None:
        """None when the endpoint is callable, otherwise a human-readable reason."""
        state = self._entitlements.state_of(endpoint)
        if state is not None:
            return f"{state.state.value} ({state.reason})"
        if not self._health.is_available(endpoint.provider):
            return "provider unavailable"
        key = (endpoint.provider_model_id, endpoint.provider)
        if self._cooldowns.is_active(key):
            remaining = self._cooldowns.remaining(key)
            return f"cooling down ({remaining:.0f}s)"
        if capability is not None and self._cooldowns.is_capability_active(key, capability):
            return f"{capability} suspended on this model"
        return None

    def retry_after(self, endpoint: Endpoint) -> float | None:
        """Seconds until this endpoint's cooldown lapses, or None if it is not timed.

        An entitlement block or a bad key has no useful countdown - it needs an
        action, not a wait - so only a real cooldown answers here.
        """
        remaining = self._cooldowns.remaining((endpoint.provider_model_id, endpoint.provider))
        return remaining if remaining > 0 else None

    def record_success(self, endpoint: Endpoint) -> None:
        self._entitlements.clear_tier(endpoint.provider, is_free=endpoint.is_free)
        self._health.record_success(endpoint.provider)
        if self._status is not None:
            self._status.mark_ok(endpoint.provider, endpoint.provider_model_id)

    def apply(self, failure: Failure, endpoint: Endpoint, *, provider_down: bool = False) -> None:
        """Update availability from one classified failure, at its own scope and no wider.

        ``provider_down`` is passed by the caller once corroboration has been
        reached; a PROVIDER_DOWN-scoped failure on its own is only a vote.
        """
        key = (endpoint.provider_model_id, endpoint.provider)
        model, provider, free = endpoint.provider_model_id, endpoint.provider, endpoint.is_free
        if failure.scope is Scope.REQUEST:
            return
        if failure.scope is Scope.MODEL_CAPABILITY and failure.capability:
            self._cooldowns.set_capability_cooldown(key, failure.capability)
            self._record(lambda s: s.record_error(provider, model, reason=failure.reason, is_free=free))
            return
        if failure.scope is Scope.MODEL:
            self._cooldowns.set_rate_limit(key, failure.retry_after)
            if failure.retry_after is not None:
                self._record(
                    lambda s: s.record_rate_limit(provider, model, retry_after=failure.retry_after, is_free=free)
                )
            else:
                self._record(lambda s: s.record_error(provider, model, reason=failure.reason, is_free=free))
            return
        if failure.scope is Scope.ACCOUNT_FREE:
            self._entitlements.free_spent(provider, failure.reason, resets_in=failure.retry_after)
            self._record(lambda s: s.record_rate_limit(provider, model, retry_after=failure.retry_after, is_free=True))
            return
        if failure.scope is Scope.ACCOUNT_PAID:
            self._entitlements.needs_billing(provider, failure.reason)
            self._record(lambda s: s.record_payment_required(provider, model, is_free=free))
            return
        if failure.scope is Scope.PROVIDER_AUTH:
            self._entitlements.forbidden(provider, failure.reason)
            self._record(lambda s: s.record_provider_down(provider, failure.reason))
            return
        if failure.scope is Scope.PROVIDER_DOWN:
            # One provider-level failure is a vote, not a verdict: it is recorded
            # against the model that saw it, and only the corroborated breaker is
            # reported provider-wide.
            if provider_down:
                self._health.mark_degraded(provider, failure.reason)
                self._record(lambda s: s.record_provider_down(provider, failure.reason))
            else:
                self._record(lambda s: s.record_error(provider, model, reason=failure.reason, is_free=free))

    def _record(self, write) -> None:
        """Report an availability change to ``north limits``, never fatally."""
        if self._status is None:
            return
        try:
            write(self._status)
        except Exception:  # pragma: no cover - reporting must not break routing
            logger.debug("Could not record availability status", exc_info=True)
