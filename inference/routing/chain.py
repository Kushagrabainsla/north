"""Building the candidate chain for a part, and walking it.

A chain is **every** model that qualifies for a part, ordered best first. Not a
top-N and not a pool: the free tier is simply the tail of the same list. The old
router kept two lists - an exclusion-filtered primary and an unfiltered
free-tier fallback - and that is how a reviewer forced off the coder's model
silently got it back the moment the primary list ran dry. One list cannot drift
out of sync with itself.

Ordering uses **one axis** per part, with cost as a filter and a tie-break.
Quality and cost are never blended into a single number - doing that trades
correctness for money at a rate nobody chose.

The order is deterministic. The same part on the same catalog produces the same
chain, which is what makes the decision log worth reading, and which also makes
per-task model stickiness redundant: the chain already yields a stable winner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from inference.facts.merge import ScorePrior
from inference.facts.models import Endpoint, ModelFacts
from inference.failure import Failure, Scope
from inference.model_policy import model_matches
from inference.routing.availability import AvailabilityView
from inference.routing.parts import REQUIREMENT_TO_FIELD, VISION, PartProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Requirements:
    """What this specific call needs, on top of what the part's profile asks for.

    Derived from the request itself - a call carrying tools needs tool support, a
    47k-token prompt needs a 47k window, ``json_mode`` needs structured output -
    so a new call site is routed sensibly without naming anything.
    """

    capabilities: frozenset[str] = frozenset()
    min_context: int = 0
    max_payload_chars: int = 0
    exclude: frozenset[str] = frozenset()

    def merged_with(self, profile: PartProfile) -> Requirements:
        return Requirements(
            capabilities=self.capabilities | profile.requires,
            min_context=max(self.min_context, profile.min_context),
            max_payload_chars=self.max_payload_chars,
            exclude=self.exclude,
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One model in a chain, with every endpoint that can serve it."""

    facts: ModelFacts
    endpoints: tuple[Endpoint, ...]
    score: float

    @property
    def canonical_id(self) -> str:
        return self.facts.canonical_id

    @property
    def price(self) -> float:
        return min((e.price for e in self.endpoints), default=float("inf"))


def context_of(facts: ModelFacts, endpoints: Sequence[Endpoint]) -> int | None:
    """The largest context window anyone claims for this model, or None if unknown."""
    windows = [w for w in (facts.value("context_window"), *(e.context_window for e in endpoints)) if w]
    return max(windows) if windows else None


def meets(facts: ModelFacts, endpoints: Sequence[Endpoint], requirements: Requirements) -> str | None:
    """None when the model qualifies, else the requirement it fails.

    Two rules meet here. A capability a source *declared false* excludes the
    model - declarations are believed in both directions. A capability nobody
    mentioned does not: unknown is not exclusion, or every model the sources
    ignore would be unreachable.
    """
    # A chain only ever serves completions and tool calls, so this is not a
    # per-part requirement - it is what a chain *is*. Embedding, transcription,
    # image and moderation models sit in the same catalogs and registries, and a
    # part that states no capability requirement would otherwise rank them
    # alongside chat models; ordered cheapest-first they win, because they are
    # cheap. Left unguarded this put Whisper at the head of the compaction chain.
    if facts.get("supports_completion") is not None and not facts.value("supports_completion"):
        return "not a completion model"
    for requirement in requirements.capabilities:
        if requirement == VISION:
            if facts.get("input_modalities") is not None and not facts.supports_vision():
                return "no image input"
            continue
        field_name = REQUIREMENT_TO_FIELD.get(requirement)
        if field_name is None:
            continue
        if facts.get(field_name) is not None and not facts.value(field_name):
            return f"no {requirement}"
    if requirements.min_context:
        window = context_of(facts, endpoints)
        if window is not None and window < requirements.min_context:
            return f"context {window:,} < {requirements.min_context:,}"
    if requirements.max_payload_chars and all(
        e.max_payload_chars is not None and e.max_payload_chars < requirements.max_payload_chars for e in endpoints
    ):
        return "payload cap too small"
    return None


def build_chain(
    profile: PartProfile,
    requirements: Requirements,
    facts: dict[str, ModelFacts],
    endpoints_by_model: dict[str, list[Endpoint]],
    prior: ScorePrior,
    *,
    floor: float | None = None,
    demoted: Callable[[str], bool] | None = None,
) -> list[Candidate]:
    """Every qualifying model for this part, ordered best first.

    *floor* is the minimum score for a cheapest-first part, already resolved from
    a percentile of the live catalog by the caller. *demoted* marks models this
    install has first-party negative evidence about; they keep their place in the
    chain's tail rather than being removed, because "worse here" is not "unusable".
    """
    combined = requirements.merged_with(profile)
    score_field = profile.order_by if profile.ranks_by_score else profile.floor_field

    candidates: list[Candidate] = []
    for canonical_id, record in facts.items():
        endpoints = endpoints_by_model.get(canonical_id)
        if not endpoints:
            continue  # no live provider serves it; a fact without an endpoint is trivia
        if canonical_id in combined.exclude:
            continue
        if meets(record, endpoints, combined) is not None:
            continue
        cheapest = min((e.price for e in endpoints), default=float("inf"))
        if profile.max_price is not None and cheapest > profile.max_price:
            continue
        raw = record.value(score_field)
        score = float(raw) if raw is not None else prior(cheapest)
        if floor is not None and score < floor:
            continue
        candidates.append(Candidate(record, tuple(endpoints), score))

    if profile.ranks_by_score:
        candidates.sort(key=lambda c: (-c.score, c.price, c.canonical_id))
    else:
        candidates.sort(key=lambda c: (c.price, -c.score, c.canonical_id))

    if demoted is not None:
        candidates = [c for c in candidates if not demoted(c.canonical_id)] + [
            c for c in candidates if demoted(c.canonical_id)
        ]
    return _pin_first(candidates, profile.pinned_model)


def _pin_first(chain: list[Candidate], pinned_model: str | None) -> list[Candidate]:
    """Move a manually pinned model to the head. A deliberate override wins."""
    if not pinned_model:
        return chain
    matched = {
        c.canonical_id
        for c in chain
        if any(model_matches(pinned_model, e.provider, e.provider_model_id) for e in c.endpoints)
    }
    if not matched:
        logger.info("Pinned model %r matches nothing in the current chain - ignoring the pin", pinned_model)
        return chain
    # Partitioned by id rather than by candidate equality: comparing whole fact
    # records for membership is quadratic over a chain of several hundred models.
    return [c for c in chain if c.canonical_id in matched] + [
        c for c in chain if c.canonical_id not in matched
    ]


@dataclass(slots=True)
class Attempt:
    """One (model, endpoint) the walk is offering. The caller reports the outcome."""

    candidate: Candidate
    endpoint: Endpoint
    failure: Failure | None = None

    def failed(self, failure: Failure) -> None:
        self.failure = failure

    @property
    def model_id(self) -> str:
        return self.endpoint.provider_model_id

    @property
    def provider(self) -> str:
        return self.endpoint.provider


@dataclass(slots=True)
class Skip:
    """One candidate the walk passed over, and why. Goes straight to the log.

    ``retry_after`` travels alongside the reason rather than inside it: a caller
    that needs to know how long to wait must never have to parse it back out of
    text written for a person to read.
    """

    model: str
    provider: str
    reason: str
    retry_after: float | None = None

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "provider": self.provider, "reason": self.reason}


@dataclass(slots=True)
class ChainWalk:
    """Walks a chain, honouring the scope of each failure.

    A failure about *this model* moves to the next model. A failure about the
    *account* or the *provider* moves to the next provider for the same model -
    the model was never the problem. A failure about the *request* is the
    caller's to handle and stops the walk.

    ``is_wired`` reports whether a provider is currently configured. An endpoint
    on a provider that is not is *skipped*, never *failed*: a model is not at
    fault for being listed against a key the user removed, and treating that as a
    model failure skipped every other endpoint it had.
    """

    chain: Sequence[Candidate]
    availability: AvailabilityView
    capability: str | None = None
    is_wired: Callable[[str], bool] | None = None
    considered: int = 0
    skipped: list[Skip] = field(default_factory=list)

    def _endpoints_of(self, candidate: Candidate) -> list[Endpoint]:
        """Cheapest healthy endpoint first - price is the only reason to prefer one."""
        return sorted(candidate.endpoints, key=lambda e: (e.price, -(e.uptime or 0.0), e.provider))

    def attempts(self) -> Iterator[Attempt]:
        for candidate in self.chain:
            self.considered += 1
            for endpoint in self._endpoints_of(candidate):
                if self.is_wired is not None and not self.is_wired(endpoint.provider):
                    self.skipped.append(
                        Skip(endpoint.provider_model_id, endpoint.provider, "provider not configured")
                    )
                    continue
                reason = self.availability.skip_reason(endpoint, self.capability)
                if reason is not None:
                    self.skipped.append(
                        Skip(
                            endpoint.provider_model_id,
                            endpoint.provider,
                            reason,
                            self.availability.retry_after(endpoint),
                        )
                    )
                    continue
                attempt = Attempt(candidate, endpoint)
                yield attempt
                if attempt.failure is None:
                    return  # the caller kept the result
                self.skipped.append(
                    Skip(
                        endpoint.provider_model_id,
                        endpoint.provider,
                        attempt.failure.reason,
                        attempt.failure.retry_after,
                    )
                )
                if attempt.failure.scope is Scope.REQUEST:
                    return
                if attempt.failure.scope in (Scope.MODEL, Scope.MODEL_CAPABILITY):
                    break  # this model is the problem; try the next model
                # ACCOUNT_PAID / PROVIDER_*: the model is fine, this provider is not.

    def soonest_retry(self) -> float | None:
        """The shortest wait any skipped candidate named, so callers back off usefully."""
        waits = [skip.retry_after for skip in self.skipped if skip.retry_after]
        return min(waits) if waits else None

    def exhaustion_summary(self, requirements: Requirements | None = None) -> str:
        """Why the whole chain came to nothing, in the words of the skips themselves.

        "47 considered: 30 need billing, 12 rate-limited, 5 context too small" is
        actionable. "All 47 candidates exhausted" is not. An empty chain is a
        different answer again - nothing *qualified* - so it names the requirement
        that emptied it rather than reporting zero of something.
        """
        if not self.chain:
            return f"no model meets this part's requirements ({_describe_requirements(requirements)})"
        if not self.skipped:
            return f"{self.considered} considered, none reachable"
        tally: dict[str, int] = {}
        for skip in self.skipped:
            tally[skip.reason] = tally.get(skip.reason, 0) + 1
        ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
        detail = ", ".join(f"{count} {reason}" for reason, count in ranked[:5])
        return f"{self.considered} considered: {detail}"


def _describe_requirements(requirements: Requirements | None) -> str:
    """The requirements in the words a person would use to check them."""
    if requirements is None:
        return "none stated"
    parts = sorted(requirements.capabilities)
    if requirements.min_context:
        parts.append(f"context >= {requirements.min_context:,}")
    if requirements.max_payload_chars:
        parts.append(f"payload <= {requirements.max_payload_chars:,} chars")
    if requirements.exclude:
        parts.append(f"excluding {', '.join(sorted(requirements.exclude))}")
    return ", ".join(parts) or "none stated"


def narrow(chain: Sequence[Candidate], requirements: Requirements) -> list[Candidate]:
    """Filter a cached chain down to what *this* call needs, preserving its order.

    A part's chain depends only on the catalog, so it is built once per refresh.
    What changes per call - the prompt's size, the models a caller excluded - only
    ever removes entries, and removing entries from an ordered list leaves it
    ordered. That keeps the hot path free of scoring, sorting and SQLite.
    """
    if not (
        requirements.capabilities
        or requirements.min_context
        or requirements.max_payload_chars
        or requirements.exclude
    ):
        return list(chain)
    return [
        candidate
        for candidate in chain
        if candidate.canonical_id not in requirements.exclude
        and meets(candidate.facts, candidate.endpoints, requirements) is None
    ]
