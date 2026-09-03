"""Merging facts from several sources, and ranking models no source scored.

Three rules do all the work:

* **Highest rank wins**, ties broken by the more recent fetch. A guess never
  displaces a declaration, however fresh it is.
* **Declared is believed in both directions.** A source that lists a model's
  parameters and does not list tools is saying it has no tools. Only ever
  *adding* capabilities is what left 66 non-tool models looking tool-capable.
* **Observation contradicts, never asserts.** Watching a call fail can demote a
  declared capability; it can never invent one, and one failure is noise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from inference.facts.models import FACT_FIELDS, Endpoint, ModelFacts, Rank, fact

# A declared capability is only demoted after this many independent failures.
# One failure is a bad gateway, a malformed request, a bad minute - not evidence
# about the model.
CONTRADICTION_THRESHOLD: int = 2


def merge(base: ModelFacts, other: ModelFacts) -> ModelFacts:
    """Field-wise merge of two fact records for the same canonical model."""
    if base.canonical_id != other.canonical_id:
        raise ValueError(f"cannot merge facts for different models: {base.canonical_id} != {other.canonical_id}")
    merged = base
    for field in FACT_FIELDS:
        incoming = other.get(field)
        if incoming is not None and incoming.beats(base.get(field)):
            merged = merged.with_fact(field, incoming)
    return merged


def merge_all(records: Iterable[ModelFacts]) -> dict[str, ModelFacts]:
    """Collapse many per-source records into one record per canonical model."""
    out: dict[str, ModelFacts] = {}
    for record in records:
        if not record.canonical_id:
            continue
        existing = out.get(record.canonical_id)
        out[record.canonical_id] = record if existing is None else merge(existing, record)
    return out


def contradict(facts: ModelFacts, field: str, source: str) -> ModelFacts:
    """Record that *field* was declared true but observably is not.

    Only meaningful for boolean capability fields, and only called once the
    caller has seen :data:`CONTRADICTION_THRESHOLD` independent failures.
    """
    return facts.with_fact(field, fact(False, Rank.OBSERVED, source, datetime.now(UTC)))


def quantization_mismatch(endpoints: Sequence[Endpoint]) -> bool:
    """True when endpoints for one model disagree about quantization.

    Two endpoints serving different quantizations of the same weights are not
    quite the same model. They are not un-merged - that would fragment the
    catalog - but the disagreement is surfaced rather than silently averaged.
    """
    declared = {e.quantization for e in endpoints if e.quantization}
    return len(declared) > 1


class ScorePrior:
    """Ranks a model no source scored, using the catalog's own distributions.

    Price is the only broad signal available for an unscored model, so the
    model's price percentile is read off as the equivalent percentile of the
    *scored* models' score distribution. Nothing is calibrated by hand: on a
    catalog where cheap models score well, a cheap unknown inherits a good
    prior, and where they do not, it does not.

    The prior is **capped at the median measured score**, so it can place an
    unknown model among the plausible but never at the head of a chain. Price is
    a weak proxy and an expensive model nobody has evaluated has not earned a
    place above a model that was actually measured - an uncapped prior put an
    unmeasured $600/Mtok model above the measured best coder in the catalog.

    It is deliberately blind to model names.
    """

    def __init__(self, prices: Sequence[float], scores: Sequence[float]) -> None:
        self._prices = sorted(p for p in prices if p is not None and p != float("inf"))
        self._scores = sorted(scores)
        self._cap = self._quantile(0.5) if self._scores else 1.0

    def __call__(self, price: float | None) -> float:
        """The prior score for a model whose cheapest endpoint costs *price*."""
        if not self._scores:
            # Nothing in the catalog is scored, so there is no distribution to
            # map onto. Fall back to the price percentile itself, which at least
            # preserves the ordering price does carry.
            return self._percentile(price)
        return min(self._quantile(self._percentile(price)), self._cap)

    def _percentile(self, price: float | None) -> float:
        """Where *price* sits among catalog prices, dearest = 1.0."""
        if price is None or price == float("inf") or not self._prices:
            return 0.5  # unknown price says nothing; sit in the middle
        below = sum(1 for p in self._prices if p < price)
        equal = sum(1 for p in self._prices if p == price)
        return (below + equal / 2) / len(self._prices)

    def _quantile(self, percentile: float) -> float:
        """The score standing at *percentile* of the scored models."""
        position = percentile * (len(self._scores) - 1)
        low = int(position)
        high = min(low + 1, len(self._scores) - 1)
        weight = position - low
        return self._scores[low] * (1 - weight) + self._scores[high] * weight


def build_prior(
    facts: Iterable[ModelFacts],
    endpoints_by_model: dict[str, list[Endpoint]],
    score_field: str,
) -> ScorePrior:
    """Build the prior for *score_field* from the live catalog."""
    prices: list[float] = []
    scores: list[float] = []
    for record in facts:
        endpoints = endpoints_by_model.get(record.canonical_id) or []
        cheapest = min((e.price for e in endpoints), default=float("inf"))
        if cheapest != float("inf"):
            prices.append(cheapest)
        score = record.value(score_field)
        if score is not None:
            scores.append(float(score))
    return ScorePrior(prices, scores)


def percentile_floor(facts: Iterable[ModelFacts], score_field: str, percentile: float) -> float:
    """The score at *percentile* of the catalog's own distribution for *score_field*.

    Quality floors are expressed as percentiles rather than absolute numbers so
    "above the floor" keeps meaning the same thing as the catalog changes under
    it, and so no threshold has to be maintained by hand.
    """
    scores = sorted(float(s) for record in facts if (s := record.value(score_field)) is not None)
    if not scores:
        return 0.0
    position = max(0.0, min(1.0, percentile)) * (len(scores) - 1)
    low = int(position)
    high = min(low + 1, len(scores) - 1)
    weight = position - low
    return scores[low] * (1 - weight) + scores[high] * weight
