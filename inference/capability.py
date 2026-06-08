"""Model capability taxonomy and per-model metadata used by ModelDispatcher."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from inference.constants import _FREE_MODEL_QUALITY, _QUALITY_LOG_MAX, _QUALITY_LOG_MIN


def quality_from_cost(cost_per_token: float) -> float:
    """Derive a 0–1 base_quality score from output token price.

    Log-scale normalisation spreads scores across the wide pricing range of
    available models (~$0.000001–$0.015/token).  Free models receive a fixed
    floor of _FREE_MODEL_QUALITY.  ModelDispatcher blends this score with a
    live per-model success-rate EMA when ranking candidates.
    """
    if cost_per_token <= 0:
        return _FREE_MODEL_QUALITY
    log_cost = math.log10(cost_per_token)
    normalised = (log_cost - _QUALITY_LOG_MIN) / (_QUALITY_LOG_MAX - _QUALITY_LOG_MIN)
    return max(0.0, min(normalised, 1.0))


class ModelCapability(StrEnum):
    COMPLETION = "completion"
    TOOL_CALLS = "tool_calls"
    EMBEDDING = "embedding"
    TRANSCRIPTION = "transcription"


def capabilities_from_model_id(model_id: str) -> frozenset[ModelCapability]:
    """Infer capabilities from naming conventions in the model ID.

    Used by providers whose API responses carry no structured capability flags.
    OpenRouter supplements this with its supported_parameters field.
    """
    lower = model_id.lower()
    if "whisper" in lower:
        return frozenset({ModelCapability.TRANSCRIPTION})
    if "embed" in lower:
        return frozenset({ModelCapability.EMBEDDING})
    return frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS})


@dataclass(frozen=True)
class ModelInfo:
    """Immutable descriptor for one model on one provider.

    base_quality is a 0–1 price-derived score.  ModelDispatcher blends it
    with a live in-memory success-rate EMA to produce effective_quality for
    candidate ranking.
    """

    model_id: str
    provider_name: str
    capabilities: frozenset[ModelCapability]
    context_window: int  # max tokens (input + output combined)
    cost_per_token: float  # USD per output token; 0.0 for free models
    base_quality: float  # 0.0–1.0 quality estimate

    @property
    def is_free(self) -> bool:
        return self.cost_per_token == 0.0

    def supports(self, capability: ModelCapability) -> bool:
        return capability in self.capabilities
