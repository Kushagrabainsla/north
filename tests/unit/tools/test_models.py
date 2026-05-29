"""Tests for tool layer models (ToolInput, ToolOutput, ConfidenceScore)."""

from __future__ import annotations

from datetime import UTC, datetime

from tools import ConfidenceScore, ToolInput, ToolOutput


def test_tool_input_defaults_to_empty_params() -> None:
    assert ToolInput().params == {}


def test_tool_input_carries_arbitrary_params() -> None:
    inp = ToolInput(params={"query": "weather", "limit": 5})
    assert inp.params == {"query": "weather", "limit": 5}


def test_tool_output_success_with_data() -> None:
    out = ToolOutput(success=True, data={"result": 42})
    assert out.success is True
    assert out.data == {"result": 42}
    assert out.error is None


def test_tool_output_failure_with_error() -> None:
    out = ToolOutput(success=False, error="rate limited")
    assert out.success is False
    assert out.data == {}
    assert out.error == "rate limited"


def test_confidence_score_carries_all_fields() -> None:
    now = datetime.now(UTC)
    score = ConfidenceScore(
        agent="finance",
        tool="market_data_api",
        confidence=0.83,
        uses_total=20,
        uses_helpful=17,
        last_updated=now,
    )
    assert score.agent == "finance"
    assert score.tool == "market_data_api"
    assert score.confidence == 0.83
    assert score.uses_total == 20
    assert score.uses_helpful == 17
