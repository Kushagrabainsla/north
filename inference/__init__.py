"""Inference layer for north - multi-provider chat completion, embeddings, and transcription.

See README Section 8 and `docs/CODING_STYLE.md` Section 6.1.
"""

from inference.base import InferenceRouter
from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.dispatcher import ModelDispatcher
from inference.exceptions import (
    AllModelsRateLimitedError,
    ContextTooLargeError,
    InferenceError,
    ModelRateLimitedError,
    PaymentRequiredError,
    PoolRefreshError,
    TranscriptionError,
)
from inference.factory import build_router
from inference.models import (
    POOL_NAMES,
    POOL_TO_PRIORITY,
    PRIORITY_TO_POOL,
    CompletionRequest,
    CompletionResponse,
    CostSummary,
    EmbedRequest,
    EmbedResponse,
    InferenceRecord,
    ModelPool,
    PoolPriority,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.provider import Provider
from inference.providers.gemini import GeminiRouter
from inference.providers.groq import GroqRouter
from inference.providers.openrouter import OpenRouterRouter

__all__ = [
    "AllModelsRateLimitedError",
    "build_router",
    "capabilities_from_model_id",
    "CompletionRequest",
    "CompletionResponse",
    "ContextTooLargeError",
    "CostSummary",
    "EmbedRequest",
    "EmbedResponse",
    "GeminiRouter",
    "GroqRouter",
    "InferenceError",
    "InferenceRecord",
    "InferenceRouter",
    "ModelCapability",
    "quality_from_cost",
    "ModelDispatcher",
    "ModelInfo",
    "ModelPool",
    "ModelRateLimitedError",
    "OpenRouterRouter",
    "POOL_NAMES",
    "POOL_TO_PRIORITY",
    "PRIORITY_TO_POOL",
    "PaymentRequiredError",
    "PoolPriority",
    "PoolRefreshError",
    "Provider",
    "ToolCall",
    "ToolCallRequest",
    "ToolCallResponse",
    "TranscriptionError",
    "TranscriptionRequest",
    "TranscriptionResponse",
]
