"""What a failed inference call is actually *about*.

Every failure has a blast radius, and the old router inferred it from the
exception's Python type. That is how a single ``401`` carrying a billing message
- one model, one unfunded account - benched an entire provider for 24 hours and
took its eight working free models with it.

Here the radius is a value, decided from the evidence the response carried, and
**wider scopes require more evidence**: ``PROVIDER_DOWN`` is unreachable from a
single call and can only be reached by corroboration across distinct models.

Providers keep raising the typed exceptions they always did - those already
carry the status code, headers and body - and this module is the single place
that reads that evidence and decides who it implicates. Keeping classification
here rather than in each provider means one table to read and one to change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from inference.exceptions import (
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


class Scope(StrEnum):
    """Who a failure implicates. Ordered narrowest to widest."""

    REQUEST = "request"  # this call only - nothing is cooled
    MODEL_CAPABILITY = "model_capability"  # this model cannot do tools / JSON
    MODEL = "model"  # 404, deprecated, model-level rate limit
    ACCOUNT_PAID = "account_paid"  # paid endpoints on this provider; free tier unaffected
    PROVIDER_AUTH = "provider_auth"  # the key is wrong - needs an action, not a timer
    PROVIDER_DOWN = "provider_down"  # only ever by corroboration


# Words that mark a response as being about money rather than about permission
# or pace. Mirrors what providers already look for when they choose between
# PaymentRequiredError and ProviderAuthError.
_BILLING_MARKERS = ("credit", "billing", "prepay", "quota", "exhausted", "insufficient", "payment")
# A 400 that names one of these is rejecting a *capability*, not the request.
_CAPABILITY_MARKERS = (
    "does not support tools",
    "tool use is not supported",
    "tools is not supported",
    "function calling",
    "response_format",
    "json_schema",
    "structured output",
    "does not support json",
)


@dataclass(frozen=True, slots=True)
class Failure:
    """One classified failure: what broke, who it implicates, and the proof."""

    scope: Scope
    reason: str
    model_id: str = ""
    provider: str = ""
    evidence: str = ""
    retry_after: float | None = None
    # The capability a MODEL_CAPABILITY failure contradicts, when known.
    capability: str | None = None

    @property
    def is_provider_wide(self) -> bool:
        return self.scope in (Scope.PROVIDER_AUTH, Scope.PROVIDER_DOWN)


def _body_text(exc: object) -> str:
    body = getattr(exc, "body", None)
    return str(body).lower() if body else ""


def _looks_like_billing(text: str) -> bool:
    return any(marker in text for marker in _BILLING_MARKERS)


def classify(
    exc: BaseException,
    *,
    model_id: str = "",
    provider: str = "",
    capability: str | None = None,
) -> Failure:
    """Decide the blast radius of *exc* from the evidence it carries.

    The mapping, signal by signal:

    ==============================  ==================  =========================================
    signal                          scope               why
    ==============================  ==================  =========================================
    401 + billing marker            ACCOUNT_PAID        Zen bills through 401; free tier unaffected
    401, no billing marker          PROVIDER_AUTH       the key is wrong
    402 / 403 + billing marker      ACCOUNT_PAID        the same entitlement fact
    429 + Retry-After               MODEL               a real, timed limit
    429, no reset, credits language ACCOUNT_PAID        Gemini reports exhaustion this way
    404                             MODEL               catalog drift
    400 rejecting tools / schema    MODEL_CAPABILITY    contradicts a declared capability
    413 payload too large           MODEL               this model cannot take north's requests
    502 / 503 / 504                 PROVIDER_DOWN       one vote toward the breaker, never a verdict
    ==============================  ==================  =========================================
    """
    model = model_id or getattr(exc, "model_id", "") or ""
    who = provider or getattr(exc, "provider_name", "") or ""
    status = getattr(exc, "status_code", None)
    evidence = f"{status or ''} {type(exc).__name__}".strip()

    if isinstance(exc, ContextTooLargeError):
        return Failure(Scope.REQUEST, "input exceeds every available context window", model, who, evidence)

    if isinstance(exc, PaymentRequiredError):
        return Failure(Scope.ACCOUNT_PAID, "account needs billing", model, who, evidence)

    if isinstance(exc, ProviderAuthError):
        # A billing message that reached here anyway is still about money.
        if _looks_like_billing(str(exc).lower() + _body_text(exc)):
            return Failure(Scope.ACCOUNT_PAID, "account needs billing", model, who, evidence)
        return Failure(Scope.PROVIDER_AUTH, "provider rejected the API key", model, who, evidence)

    if isinstance(exc, ModelRateLimitedError):
        if exc.retry_after is None and _looks_like_billing(_body_text(exc)):
            return Failure(Scope.ACCOUNT_PAID, "rate limit with no reset and credits language", model, who, evidence)
        return Failure(
            Scope.MODEL,
            "model is rate limited",
            model,
            who,
            evidence,
            retry_after=exc.retry_after,
        )

    if isinstance(exc, ModelNotFoundError):
        return Failure(Scope.MODEL, "model not found (catalog drift)", model, who, evidence)

    if isinstance(exc, PayloadTooLargeError):
        return Failure(Scope.MODEL, "model rejected the request size", model, who, evidence)

    if isinstance(exc, ProviderUnavailableError):
        return Failure(Scope.PROVIDER_DOWN, "gateway or server outage", model, who, evidence)

    if isinstance(exc, ModelDegenerateError):
        return Failure(
            Scope.MODEL_CAPABILITY if capability else Scope.MODEL,
            f"degenerate response: {exc.reason}",
            model,
            who,
            evidence,
            capability=capability,
        )

    if isinstance(exc, InferenceError):
        text = str(exc).lower()
        if any(marker in text for marker in _CAPABILITY_MARKERS):
            return Failure(
                Scope.MODEL_CAPABILITY,
                "provider rejected a declared capability",
                model,
                who,
                evidence,
                capability=capability,
            )
        return Failure(Scope.MODEL, str(exc)[:160] or "inference error", model, who, evidence)

    return Failure(Scope.MODEL, f"{type(exc).__name__}: {str(exc)[:120]}", model, who, evidence)


def invalid_response(model_id: str, provider: str, capability: str | None) -> Failure:
    """A 200 OK that did not carry the answer that was asked for.

    Not an exception, but a failure all the same: an empty completion, or JSON
    that ignored the schema. It contradicts a capability when one was requested,
    and is otherwise just this model behaving badly.
    """
    return Failure(
        Scope.MODEL_CAPABILITY if capability else Scope.MODEL,
        "empty or invalid response",
        model_id,
        provider,
        evidence="200 without usable content",
        capability=capability,
    )


@dataclass
class _ProviderVotes:
    models: dict[str, float] = field(default_factory=dict)


class OutageCorroboration:
    """Counts provider-level failure votes, so a breaker needs a real quorum.

    ``PROVIDER_DOWN`` is the widest scope there is, so one 503 - which is often
    one bad upstream behind a gateway - must not reach it. A provider is only
    declared down once :attr:`quorum` *distinct models* have failed with a
    provider-level signal inside :attr:`window_seconds`.
    """

    def __init__(self, *, quorum: int = 3, window_seconds: float = 120.0) -> None:
        self.quorum = max(2, quorum)
        self.window_seconds = window_seconds
        self._votes: dict[str, _ProviderVotes] = {}

    def record(self, provider: str, model_id: str) -> bool:
        """Register one vote. True when the provider is now corroborated as down."""
        now = time.monotonic()
        votes = self._votes.setdefault(provider, _ProviderVotes())
        votes.models = {m: t for m, t in votes.models.items() if now - t <= self.window_seconds}
        votes.models[model_id] = now
        return len(votes.models) >= self.quorum

    def clear(self, provider: str) -> None:
        """A success is evidence the provider is up. Drop its votes."""
        self._votes.pop(provider, None)

    def votes(self, provider: str) -> int:
        now = time.monotonic()
        votes = self._votes.get(provider)
        if votes is None:
            return 0
        return sum(1 for t in votes.models.values() if now - t <= self.window_seconds)
