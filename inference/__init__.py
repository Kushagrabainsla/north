"""Inference Router for north — chat completion and audio transcription via OpenRouter.

See README Section 8 and `docs/CODING_STYLE.md` Section 6.1.
"""

from inference.base import InferenceRouter
from inference.exceptions import (
    AllModelsRateLimitedError,
    InferenceError,
    PoolRefreshError,
    TranscriptionError,
)
from inference.fallback_pools import DEFAULT_TRANSCRIPTION_MODEL, FALLBACK_POOLS
from inference.models import (
    POOL_NAMES,
    POOL_TO_PRIORITY,
    PRIORITY_TO_POOL,
    CompletionRequest,
    CompletionResponse,
    CostSummary,
    InferenceRecord,
    ModelPool,
    PoolPriority,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.openrouter import OpenRouterInferenceRouter

__all__ = [
    "AllModelsRateLimitedError",
    "CompletionRequest",
    "CompletionResponse",
    "CostSummary",
    "DEFAULT_TRANSCRIPTION_MODEL",
    "FALLBACK_POOLS",
    "InferenceError",
    "InferenceRecord",
    "InferenceRouter",
    "ModelPool",
    "OpenRouterInferenceRouter",
    "POOL_NAMES",
    "POOL_TO_PRIORITY",
    "PRIORITY_TO_POOL",
    "PoolPriority",
    "PoolRefreshError",
    "TranscriptionError",
    "TranscriptionRequest",
    "TranscriptionResponse",
]
