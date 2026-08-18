"""Unit tests for the price-free ModelScorer.

Verifies the three-signal score (family tier + live EMA + curation boost) and
that unknown families fall back to the config floor instead of collapsing.
"""

from __future__ import annotations

from pathlib import Path

from config.strategy import StrategyMode
from inference.model_scorer import ModelScorer, ScoringConfig


def _scorer(weights=None, tiers=None) -> ModelScorer:
    cfg = weights or ScoringConfig()
    s = ModelScorer(config=cfg, tiers_path=Path("/nonexistent/model_tiers.json"))
    if tiers is not None:
        s._tiers = dict(tiers)
    return s


def test_family_tier_longest_match_wins():
    s = _scorer(tiers={"opus": 0.9, "claude-opus-4": 0.96})
    # "claude-opus-4" should match the longer key, not "opus".
    assert s.family_tier("anthropic/claude-opus-4-8") == 0.96


def test_unknown_family_falls_back_to_floor():
    s = _scorer(weights=ScoringConfig(unknown_family_quality=0.35))
    assert s.family_tier("some-unknown-model-xyz") == 0.35


def test_score_rewards_strong_family_over_weak():
    s = _scorer()
    strong = s.score("claude-opus-4-8", ema_score=0.5, is_preferred=False)
    weak = s.score("llama-3.1-8b-instant", ema_score=0.5, is_preferred=False)
    assert strong > weak


def test_curation_boost_raises_score():
    s = _scorer()
    base = s.score("llama-3.1-8b-instant", ema_score=0.5, is_preferred=False)
    boosted = s.score("llama-3.1-8b-instant", ema_score=0.5, is_preferred=True)
    assert boosted > base


def test_live_ema_rises_score():
    s = _scorer()
    low = s.score("claude-opus-4-8", ema_score=0.2, is_preferred=False)
    high = s.score("claude-opus-4-8", ema_score=0.9, is_preferred=False)
    assert high > low


def test_sport_emphasises_capability():
    s = _scorer()
    cruise = s.score("claude-opus-4-8", ema_score=0.5, is_preferred=False, power=StrategyMode.CRUISE)
    sport = s.score("claude-opus-4-8", ema_score=0.5, is_preferred=False, power=StrategyMode.SPORT)
    # SPORT scales family/ema up, so a top model's score should be >= cruise.
    assert sport >= cruise


def test_tiers_override_file_merges(tmp_path):
    # Test that ScoringConfig.family_tiers merges with built-in defaults
    cfg = ScoringConfig(family_tiers={"custom-strong": 0.99})
    s = ModelScorer(config=cfg, tiers_path=Path("/nonexistent/model_tiers.json"))
    assert s.family_tier("provider/custom-strong-model") == 0.99
    # built-in still present
    assert s.family_tier("claude-opus-4-8") == 0.96


def test_scoring_config_roundtrip():
    raw = {"family_weight": 0.6, "ema_weight": 0.3, "curation_weight": 0.1, "unknown_family_quality": 0.4}
    cfg = ScoringConfig.from_dict(raw)
    # to_dict() now includes family_tiers (merged with built-in defaults)
    result = cfg.to_dict()
    assert result["family_weight"] == 0.6
    assert result["ema_weight"] == 0.3
    assert result["curation_weight"] == 0.1
    assert result["unknown_family_quality"] == 0.4
    assert "family_tiers" in result
    # Should contain built-in defaults
    assert "claude-opus-4" in result["family_tiers"]
