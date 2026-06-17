"""Inference-layer exceptions."""

from __future__ import annotations

from exceptions import NorthError


class InferenceError(NorthError):
    """Base class for inference-layer failures."""


class AllModelsRateLimitedError(InferenceError):
    """Every candidate in the dispatch chain was exhausted."""


class ModelRateLimitedError(InferenceError):
    """A specific (model, provider) pair returned a rate-limit response.

    ModelDispatcher catches this, applies a cooldown to that pair, and tries
    the next candidate in the chain. Never surfaces to callers.
    """

    def __init__(self, model_id: str, provider_name: str) -> None:
        super().__init__(f"Rate limited: {model_id} on {provider_name}")
        self.model_id = model_id
        self.provider_name = provider_name


class PaymentRequiredError(InferenceError):
    """A provider returned 402 - account has insufficient credits.

    ModelDispatcher applies a long cooldown to that (model, provider) pair
    and continues to the next candidate.
    """


class ContextTooLargeError(InferenceError):
    """Input exceeds every available model's context window.

    Raised by ModelDispatcher when no candidate survives the context filter.
    AgenticLLMAgent catches this, compacts history to keep_recent=1, and retries.
    """

    def __init__(self, estimated_tokens: int, largest_context: int) -> None:
        super().__init__(
            f"Input (~{estimated_tokens:,} tokens) exceeds the largest available "
            f"context window ({largest_context:,} tokens) - compact and retry"
        )
        self.estimated_tokens = estimated_tokens
        self.largest_context = largest_context


class PoolRefreshError(InferenceError):
    """Failed to fetch the live model list from a provider."""


class TranscriptionError(InferenceError):
    """Audio transcription failed."""
