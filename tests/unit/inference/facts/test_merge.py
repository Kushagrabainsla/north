"""Rank-based merging, and the prior that ranks models no source scored."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inference.facts.merge import ScorePrior, build_prior, merge, merge_all, percentile_floor
from inference.facts.models import Endpoint, ModelFacts, Rank, fact

_EARLY = datetime(2026, 1, 1, tzinfo=UTC)
_LATE = _EARLY + timedelta(days=1)


def test_higher_rank_wins_regardless_of_age() -> None:
    declared = ModelFacts("m", context_window=fact(200_000, Rank.DECLARED, "litellm", _EARLY))
    inferred = ModelFacts("m", context_window=fact(128_000, Rank.INFERRED, "heuristic", _LATE))
    assert merge(declared, inferred).value("context_window") == 200_000
    assert merge(inferred, declared).value("context_window") == 200_000


def test_recency_only_breaks_a_tie() -> None:
    old = ModelFacts("m", context_window=fact(128_000, Rank.DECLARED, "a", _EARLY))
    new = ModelFacts("m", context_window=fact(200_000, Rank.DECLARED, "b", _LATE))
    assert merge(old, new).value("context_window") == 200_000


def test_declared_false_survives_a_merge() -> None:
    """A source that lists parameters and omits tools is saying "no tools"."""
    says_no = ModelFacts("m", supports_tools=fact(False, Rank.DECLARED, "openrouter", _LATE))
    guesses_yes = ModelFacts("m", supports_tools=fact(True, Rank.INFERRED, "heuristic", _LATE))
    assert merge(says_no, guesses_yes).value("supports_tools") is False


def test_facts_from_several_sources_collapse_onto_one_record() -> None:
    records = [
        ModelFacts("gpt-5-codex", context_window=fact(272_000, Rank.DECLARED, "litellm", _EARLY)),
        ModelFacts("gpt-5-codex", supports_tools=fact(True, Rank.DECLARED, "litellm", _EARLY)),
        ModelFacts("claude-opus-5", coding_score=fact(0.78, Rank.DECLARED, "openrouter", _EARLY)),
    ]
    merged = merge_all(records)
    assert set(merged) == {"gpt-5-codex", "claude-opus-5"}
    assert merged["gpt-5-codex"].value("context_window") == 272_000
    assert merged["gpt-5-codex"].value("supports_tools") is True


def test_prior_never_outranks_a_measurement() -> None:
    """An expensive model nobody evaluated must not lead a chain of measured ones."""
    prior = ScorePrior(prices=[1.0, 2.0, 3.0, 600.0], scores=[0.5, 0.6, 0.78, 0.816])
    assert prior(600.0) <= max([0.5, 0.6, 0.78, 0.816])
    assert prior(600.0) <= 0.69  # the median of the measured scores


def test_prior_falls_back_to_price_when_nothing_is_measured() -> None:
    prior = ScorePrior(prices=[1.0, 2.0, 3.0], scores=[])
    assert prior(3.0) > prior(1.0)


def test_unknown_price_sits_in_the_middle() -> None:
    prior = ScorePrior(prices=[1.0, 100.0], scores=[0.2, 0.4, 0.9])
    assert prior(None) == prior(float("inf"))


def test_floors_are_percentiles_of_the_live_distribution() -> None:
    facts = [
        ModelFacts(f"m{i}", intelligence_score=fact(score, Rank.DECLARED, "s", _EARLY))
        for i, score in enumerate([0.1, 0.3, 0.5, 0.7, 0.9])
    ]
    assert percentile_floor(facts, "intelligence_score", 0.5) == 0.5
    assert percentile_floor(facts, "intelligence_score", 0.0) == 0.1
    assert percentile_floor([], "intelligence_score", 0.5) == 0.0


def test_build_prior_uses_the_cheapest_endpoint_per_model() -> None:
    facts = [ModelFacts("a", coding_score=fact(0.8, Rank.DECLARED, "s", _EARLY)), ModelFacts("b")]
    endpoints = {
        "a": [Endpoint("a", "p", "a", price_out=10.0)],
        "b": [Endpoint("b", "p", "b", price_out=5.0), Endpoint("b", "q", "b", price_out=1.0)],
    }
    prior = build_prior(facts, endpoints, "coding_score")
    assert 0.0 <= prior(1.0) <= 0.8
