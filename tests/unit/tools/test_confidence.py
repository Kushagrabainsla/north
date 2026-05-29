"""Tests for ConfidenceTracker — EMA score math, persistence, agent inheritance."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import (
    DEFAULT_CONFIDENCE,
    EMA_ALPHA,
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
    ConfidenceTracker,
)


# Convenience: one EMA step from the default prior toward a given outcome.
def _ema(prior: float, outcome: float) -> float:
    return EMA_ALPHA * outcome + (1.0 - EMA_ALPHA) * prior


@pytest.fixture
def tracker(tmp_path: Path) -> ConfidenceTracker:
    return ConfidenceTracker(tmp_path / "tools.db")


async def test_score_for_unknown_pair_returns_default(
    tracker: ConfidenceTracker,
) -> None:
    assert await tracker.get_score("health", "nutrition_api") == DEFAULT_CONFIDENCE


async def test_helpful_use_increases_score(tracker: ConfidenceTracker) -> None:
    expected = _ema(DEFAULT_CONFIDENCE, 1.0)
    new = await tracker.record_use("health", "web_search", was_helpful=True)
    assert new == pytest.approx(expected)
    assert await tracker.get_score("health", "web_search") == pytest.approx(expected)


async def test_unhelpful_use_decreases_score(tracker: ConfidenceTracker) -> None:
    expected = _ema(DEFAULT_CONFIDENCE, 0.0)
    new = await tracker.record_use("health", "web_search", was_helpful=False)
    assert new == pytest.approx(expected)


async def test_repeated_helpful_use_approaches_max(tracker: ConfidenceTracker) -> None:
    # EMA converges asymptotically — after 50 helpful steps the score is > 0.99.
    for _ in range(50):
        await tracker.record_use("finance", "market_data_api", was_helpful=True)
    score = await tracker.get_score("finance", "market_data_api")
    assert score >= 0.99
    assert score <= MAX_CONFIDENCE


async def test_repeated_unhelpful_use_approaches_min(
    tracker: ConfidenceTracker,
) -> None:
    # EMA converges asymptotically — after 50 unhelpful steps the score is < 0.01.
    for _ in range(50):
        await tracker.record_use("job", "linkedin_api", was_helpful=False)
    score = await tracker.get_score("job", "linkedin_api")
    assert score <= 0.01
    assert score >= MIN_CONFIDENCE


async def test_use_counters_increment(tracker: ConfidenceTracker) -> None:
    # 2 helpful then 1 unhelpful via EMA
    s = DEFAULT_CONFIDENCE
    s = _ema(s, 1.0)
    s = _ema(s, 1.0)
    s = _ema(s, 0.0)
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=False)
    scores = await tracker.scores_for_agent("health")
    assert dict(scores)["web_search"] == pytest.approx(s, rel=1e-5)


async def test_scores_for_agent_orders_by_confidence_descending(
    tracker: ConfidenceTracker,
) -> None:
    # web_search: 2 helpful → highest
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=True)
    # calendar_api: 1 unhelpful → lowest
    await tracker.record_use("health", "calendar_api", was_helpful=False)
    # nutrition_api: 1 helpful → middle
    await tracker.record_use("health", "nutrition_api", was_helpful=True)

    scores = await tracker.scores_for_agent("health")
    assert [name for name, _ in scores] == [
        "web_search",
        "nutrition_api",
        "calendar_api",
    ]


async def test_scores_for_agent_returns_empty_when_unseen(
    tracker: ConfidenceTracker,
) -> None:
    assert await tracker.scores_for_agent("never_seen_agent") == []


async def test_inherit_from_copies_source_scores_to_new_agent(
    tracker: ConfidenceTracker,
) -> None:
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "nutrition_api", was_helpful=True)

    await tracker.inherit_from("wellness", "health")

    inherited = dict(await tracker.scores_for_agent("wellness"))
    source = dict(await tracker.scores_for_agent("health"))
    assert inherited == source


async def test_inherit_from_is_idempotent_and_preserves_existing_rows(
    tracker: ConfidenceTracker,
) -> None:
    await tracker.record_use("health", "web_search", was_helpful=True)
    expected_wellness = _ema(DEFAULT_CONFIDENCE, 0.0)
    await tracker.record_use("wellness", "web_search", was_helpful=False)

    await tracker.inherit_from("wellness", "health")

    assert (
        await tracker.get_score("wellness", "web_search") == pytest.approx(expected_wellness)
    )


async def test_persistence_across_tracker_instances(tmp_path: Path) -> None:
    """Scores must persist across processes — write with one instance, read with another."""
    db = tmp_path / "tools.db"
    t1 = ConfidenceTracker(db)
    expected = _ema(DEFAULT_CONFIDENCE, 1.0)
    await t1.record_use("health", "web_search", was_helpful=True)

    t2 = ConfidenceTracker(db)
    assert (
        await t2.get_score("health", "web_search") == pytest.approx(expected)
    )
