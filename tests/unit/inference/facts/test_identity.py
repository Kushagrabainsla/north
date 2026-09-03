"""Canonical identity must merge the same model and never merge different ones."""

from __future__ import annotations

import pytest

from inference.facts.identity import canonical, variant_of


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("openai/gpt-5.1-codex", "gpt-5-1-codex"),
        ("gpt-5.1-codex", "gpt-5-1-codex"),  # joins the row above
        ("anthropic.claude-opus-5", "claude-opus-5"),  # LiteLLM vendor dot-prefix
        ("minimax/minimax-m3:free", "minimax-m3"),  # :free is an endpoint, not a model
        ("gemini-3.1-pro-preview", "gemini-3-1-pro"),
        ("glm-5-free", "glm-5"),  # Zen writes its free tier as a suffix
    ],
)
def test_canonical_merges_the_same_model(model_id: str, expected: str) -> None:
    assert canonical(model_id) == expected


def test_size_tokens_are_never_stripped() -> None:
    """``-max`` is not a variant suffix; merging these would misroute the coder."""
    assert canonical("gpt-5.1-codex-max") != canonical("gpt-5.1-codex")
    assert canonical("gpt-5.1-codex-max") == "gpt-5-1-codex-max"


def test_endpoint_variants_are_recognised() -> None:
    assert variant_of("z-ai/glm-5:free") == "free"
    assert variant_of("anthropic/claude-opus-5:batch") == "batch"
    assert variant_of("anthropic/claude-opus-5") is None
