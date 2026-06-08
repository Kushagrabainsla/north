"""Models and enums for the Inference Router. See README Section 8."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PoolPriority(StrEnum):
    """Priority signal that determines which model pool a call uses.

    See README 8.3 for the mapping rules (consequential→HIGH, background→LOW).
    """

    HIGH = "high"  # reasoning pool
    MEDIUM = "medium"  # fast_cheap pool
    LOW = "low"  # high_volume pool


# Canonical pool names. PoolPriority maps onto these one-to-one.
POOL_NAMES = ("reasoning", "fast_cheap", "high_volume")

PRIORITY_TO_POOL: dict[PoolPriority, str] = {
    PoolPriority.HIGH: "reasoning",
    PoolPriority.MEDIUM: "fast_cheap",
    PoolPriority.LOW: "high_volume",
}

# Reverse map: agent config.yaml uses pool names; the router takes priorities.
POOL_TO_PRIORITY: dict[str, PoolPriority] = {v: k for k, v in PRIORITY_TO_POOL.items()}


class ModelEntry(BaseModel):
    """One model entry in a pool, carrying its router/provider alongside the model ID."""

    id: str
    provider: str


class ModelPool(BaseModel):
    """One pool of models. During routing the dispatcher picks randomly within each quality tier."""

    name: str
    models: list[ModelEntry]


class CompletionRequest(BaseModel):
    """Input to a chat-completion call."""

    prompt: str
    priority: PoolPriority = PoolPriority.MEDIUM
    component: str
    task_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    # When True the provider is instructed to return valid JSON (response_format
    # json_object).  Only set this when the system prompt guarantees JSON output.
    json_mode: bool = False


class CompletionResponse(BaseModel):
    """Output of a chat-completion call. The Orchestrator logs this to the Ledger."""

    text: str
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class ToolCallRequest(BaseModel):
    """Input to a function-calling completion.

    ``messages`` is the full OpenAI-format conversation history.
    ``tools`` is the list of OpenAI-format function definitions to offer.
    """

    messages: list[dict]
    tools: list[dict]
    priority: PoolPriority = PoolPriority.MEDIUM
    component: str
    task_id: str | None = None


class ToolCall(BaseModel):
    """One function invocation from a model response."""

    name: str
    call_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    """Result of a single function-calling turn.

    ``type`` is ``"tool_calls"`` when the model invoked one or more functions,
    or ``"message"`` when it produced a final text answer.
    ``calls`` carries every tool the model dispatched in this turn (often one,
    but models can and do issue multiple calls in parallel).
    """

    type: str  # "tool_calls" | "message"
    content: str | None = None
    calls: list[ToolCall] = []
    model_used: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class TranscriptionRequest(BaseModel):
    """Input to an audio-transcription call. See README 8.6 and 16.6."""

    # Pydantic v2 supports `bytes` directly. Callers pass raw audio.
    audio: bytes
    model: str | None = None  # None → router uses its configured default
    component: str = "perception"
    task_id: str | None = None


class TranscriptionResponse(BaseModel):
    text: str
    model_used: str
    cost_usd: float = 0.0


class EmbedRequest(BaseModel):
    texts: list[str]
    component: str = "embed"
    task_id: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model_used: str
    cost_usd: float = 0.0


class InferenceRecord(BaseModel):
    """One inference call. Written to the Ledger with source=inference_router."""

    component: str
    priority: PoolPriority | None = None  # None for transcription
    model_used: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float
    task_id: str | None = None


class CostSummary(BaseModel):
    """Aggregated inference costs over a window. Returned by the `costs` CLI command."""

    period: str
    total_cost_usd: float
    by_component: dict[str, float] = Field(default_factory=dict)
    by_model: dict[str, float] = Field(default_factory=dict)
