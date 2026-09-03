"""The vocabulary of fetched model data: what is known, how well, and from where.

Two record types, deliberately separate:

``ModelFacts``  what a model *is* - context window, capabilities, quality scores.
                Provider independent, so a fact learned from one source applies
                wherever that model is served.
``Endpoint``    the *terms* of serving it - price, limits, entitlement. Never
                crosses providers: borrowing OpenRouter's price for a Zen call
                would be a lie, and that separation is what makes borrowing
                facts safe.

Every fact carries its :class:`Rank`, so a merge never has to guess which of two
disagreeing sources to believe, and a wrong value stays traceable to whoever
supplied it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any


class Rank(IntEnum):
    """How much weight a value carries. Higher wins a merge."""

    UNKNOWN = 0
    INFERRED = 1  # guessed from the model id - last resort
    DECLARED = 2  # a catalog source said so
    OBSERVED = 3  # north watched it fail; only ever contradicts, never asserts


class Entitlement(StrEnum):
    """Whether this account may currently use an endpoint.

    A property of the *account*, not of the model or the provider: "no payment
    method" says nothing about the free tier served from the same host.
    """

    OK = "OK"
    NEEDS_BILLING = "NEEDS_BILLING"
    FORBIDDEN = "FORBIDDEN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Fact[T]:
    """One value plus the provenance needed to merge and to audit it."""

    value: T
    rank: Rank
    source: str
    fetched_at: datetime

    def beats(self, other: Fact[Any] | None) -> bool:
        """True when this fact should win a merge against *other*.

        Rank first, recency only to break a tie - a newer guess must never
        displace an older declaration.
        """
        if other is None:
            return True
        if self.rank != other.rank:
            return self.rank > other.rank
        return self.fetched_at > other.fetched_at


def fact[T](value: T, rank: Rank, source: str, fetched_at: datetime | None = None) -> Fact[T]:
    """Build a :class:`Fact`, defaulting *fetched_at* to now."""
    return Fact(value=value, rank=rank, source=source, fetched_at=fetched_at or datetime.now(UTC))


# Every merged/stored field on ModelFacts, in one place: merge, persistence and
# provenance all iterate this rather than each repeating the field list.
FACT_FIELDS: tuple[str, ...] = (
    "context_window",
    "supports_completion",
    "max_output_tokens",
    "supports_tools",
    "supports_reasoning",
    "supports_structured",
    "input_modalities",
    "coding_score",
    "agentic_score",
    "intelligence_score",
)

# The fields a part profile may order a chain by.
SCORE_FIELDS: tuple[str, ...] = ("coding_score", "agentic_score", "intelligence_score")


@dataclass(frozen=True, slots=True)
class ModelFacts:
    """What is known about one canonical model, independent of who serves it.

    Every field is optional: a model no source describes is ranked by a prior,
    never excluded. Absence is "unknown", which is not the same as "no" - only a
    source that positively declares ``false`` makes a capability false.
    """

    canonical_id: str
    context_window: Fact[int] | None = None
    # Whether this model answers chat/completion requests at all. Embedding,
    # transcription, image and moderation models are in the same catalogs and the
    # same provider registries, and a part that states no capability requirement
    # would otherwise rank them alongside chat models - on a paid-only install
    # that put Whisper at the head of the compaction chain.
    supports_completion: Fact[bool] | None = None
    max_output_tokens: Fact[int] | None = None
    supports_tools: Fact[bool] | None = None
    supports_reasoning: Fact[bool] | None = None
    supports_structured: Fact[bool] | None = None
    input_modalities: Fact[frozenset[str]] | None = None
    coding_score: Fact[float] | None = None
    agentic_score: Fact[float] | None = None
    intelligence_score: Fact[float] | None = None

    def get(self, field: str) -> Fact[Any] | None:
        return getattr(self, field, None)

    def value(self, field: str, default: Any = None) -> Any:
        """The value of *field*, or *default* when nothing is known about it."""
        found = self.get(field)
        return default if found is None else found.value

    def declares(self, field: str) -> bool:
        """True when a source positively asserted this field (either way)."""
        found = self.get(field)
        return found is not None and found.rank >= Rank.DECLARED

    def with_fact(self, field: str, value: Fact[Any] | None) -> ModelFacts:
        return replace(self, **{field: value})

    def provenance(self) -> dict[str, dict[str, str]]:
        """Per-field ``{rank, source, fetched_at}``, for storage and for auditing."""
        out: dict[str, dict[str, str]] = {}
        for field in FACT_FIELDS:
            found = self.get(field)
            if found is not None:
                out[field] = {
                    "rank": found.rank.name,
                    "source": found.source,
                    "fetched_at": found.fetched_at.isoformat(),
                }
        return out

    def supports_vision(self) -> bool:
        modalities = self.value("input_modalities")
        return bool(modalities) and "image" in modalities


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One way to actually call a model: a provider, an id to send, and its terms.

    ``provider_model_id`` is the exact string that goes on the wire. It is kept
    verbatim rather than reconstructed from the canonical id, because
    canonicalisation is lossy by design.
    """

    canonical_id: str
    provider: str
    provider_model_id: str
    price_in: float | None = None
    price_out: float | None = None
    quantization: str | None = None
    max_payload_chars: int | None = None
    entitlement: Entitlement = Entitlement.UNKNOWN
    uptime: float | None = None
    context_window: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.canonical_id, self.provider, self.provider_model_id)

    @property
    def price(self) -> float:
        """Output price per token - the axis every cost comparison here uses.

        Unknown price sorts last rather than free: treating "we were not told"
        as "it costs nothing" is how a cheap part reaches an expensive model.
        """
        if self.price_out is not None:
            return self.price_out
        if self.price_in is not None:
            return self.price_in
        return float("inf")

    @property
    def is_free(self) -> bool:
        """True only when a price was published and it is zero.

        Read from :attr:`price` so "free" can never disagree with how the endpoint
        is ordered: an unknown price is not free, and a zero price is not paid.
        """
        return self.price == 0.0
