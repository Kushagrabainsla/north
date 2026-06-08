"""Provider protocol — the contract individual inference providers must satisfy.

Providers are called only by ModelDispatcher. External code talks exclusively
to InferenceRouter (implemented by ModelDispatcher), never to Provider directly.
Each method receives an explicit model_id; the dispatcher owns model selection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from inference.capability import ModelInfo
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)


@runtime_checkable
class Provider(Protocol):
    """Single inference provider. The dispatcher calls these methods directly."""

    name: str  # human-readable identifier, e.g. "openrouter", "groq", "gemini"

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse: ...

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse: ...

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse: ...

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse: ...

    def get_models(self) -> dict[str, ModelInfo]:
        """Return all models this provider can currently serve, keyed by model_id."""
        ...

    async def refresh(self) -> None:
        """Re-fetch supported models from the provider's API."""
        ...
