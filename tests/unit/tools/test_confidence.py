"""Tests for ConfidenceTracker — score math, persistence, agent inheritance."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import (
    CONFIDENCE_DECREASE,
    CONFIDENCE_INCREASE,
    ConfidenceTracker,
    DEFAULT_CONFIDENCE,
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
)


@pytest.fixture
def tracker(tmp_path: Path) -> ConfidenceTracker:
    return ConfidenceTracker(tmp_path / "tools.db")


async def test_score_for_unknown_pair_returns_default(
    tracker: ConfidenceTracker,
) -> None:
    assert await tracker.get_score("health", "nutrition_api") == DEFAULT_CONFIDENCE


async def test_helpful_use_increases_score(tracker: ConfidenceTracker) -> None:
    new = await tracker.record_use("health", "web_search", was_helpful=True)
    assert new == pytest.approx(DEFAULT_CONFIDENCE + CONFIDENCE_INCREASE)
    assert (
        await tracker.get_score("health", "web_search")
        == pytest.approx(DEFAULT_CONFIDENCE + CONFIDENCE_INCREASE)
    )


async def test_unhelpful_use_decreases_score(tracker: ConfidenceTracker) -> None:
    new = await tracker.record_use("health", "web_search", was_helpful=False)
    assert new == pytest.approx(DEFAULT_CONFIDENCE - CONFIDENCE_DECREASE)


async def test_repeated_helpful_use_caps_at_one(tracker: ConfidenceTracker) -> None:
    for _ in range(50):
        await tracker.record_use("finance", "market_data_api", was_helpful=True)
    assert await tracker.get_score("finance", "market_data_api") == MAX_CONFIDENCE


async def test_repeated_unhelpful_use_floors_at_zero(
    tracker: ConfidenceTracker,
) -> None:
    for _ in range(50):
        await tracker.record_use("job", "linkedin_api", was_helpful=False)
    assert await tracker.get_score("job", "linkedin_api") == MIN_CONFIDENCE


async def test_use_counters_increment(tracker: ConfidenceTracker) -> None:
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=False)

    scores = await tracker.scores_for_agent("health")
    # 0.5 + 0.05 + 0.05 - 0.03 = 0.57
    assert dict(scores)["web_search"] == pytest.approx(0.57)


async def test_scores_for_agent_orders_by_confidence_descending(
    tracker: ConfidenceTracker,
) -> None:
    await tracker.record_use("health", "web_search", was_helpful=True)
    await tracker.record_use("health", "web_search", was_helpful=True)  # 0.6
    await tracker.record_use("health", "calendar_api", was_helpful=False)  # 0.47
    await tracker.record_use("health", "nutrition_api", was_helpful=True)  # 0.55

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
    await tracker.record_use("health", "web_search", was_helpful=True)  # 0.55
    # wellness already has its own opinion of web_search:
    await tracker.record_use("wellness", "web_search", was_helpful=False)  # 0.47

    await tracker.inherit_from("wellness", "health")

    # wellness's existing row was preserved, not overwritten.
    assert (
        await tracker.get_score("wellness", "web_search") == pytest.approx(0.47)
    )


async def test_persistence_across_tracker_instances(tmp_path: Path) -> None:
    """Scores must persist across processes — write with one instance, read with another."""
    db = tmp_path / "tools.db"
    t1 = ConfidenceTracker(db)
    await t1.record_use("health", "web_search", was_helpful=True)

    t2 = ConfidenceTracker(db)
    assert (
        await t2.get_score("health", "web_search")
        == pytest.approx(DEFAULT_CONFIDENCE + CONFIDENCE_INCREASE)
    )
