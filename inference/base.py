"""Abstract interface for the Inference Router. See README Section 8."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)


class InferenceRouter(ABC):
    """Routes all LLM and audio calls through one provider.

    The router selects a model based on a priority signal (HIGH→reasoning,
    MEDIUM→fast_cheap, LOW→high_volume), retries on rate-limit by walking
    down the same pool, and refreshes pool membership periodically. It
    owns both chat completion and audio transcription (Decision 3) so the
    rest of the system stays single-provider.
    """

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one chat-completion call. Retries within the requested pool on
        rate limit; raises `AllModelsRateLimitedError` if every model is throttled."""

    @abstractmethod
    async def transcribe(
        self, request: TranscriptionRequest
    ) -> TranscriptionResponse:
        """Run one audio-transcription call via the same provider."""

    @abstractmethod
    async def get_model(self, priority: PoolPriority) -> str:
        """Return the current primary model for `priority`."""

    @abstractmethod
    async def refresh_pools(self) -> None:
        """Re-fetch the model list from the provider and rebuild pools.

        Persists the result to the cache file on success. On failure, the
        previously cached pools (or the hardcoded fallback) are retained.
        """

    @abstractmethod
    def current_pools(self) -> dict[str, ModelPool]:
        """Snapshot of the current pool state. Powers the `inference models` CLI."""

    @abstractmethod
    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        """Function-calling completion.  The model either returns a tool call or
        a final text message.  When *token_callback* is provided, text tokens are
        forwarded to it as they stream in (task 4 streaming).
        """

    @abstractmethod
    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        """Embed a batch of texts and return one float vector per input."""
