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

    def __init__(
        self,
        model_id: str,
        provider_name: str,
        retry_after: float | None = None,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__(f"Rate limited: {model_id} on {provider_name}")
        self.model_id = model_id
        self.provider_name = provider_name
        # Seconds the provider asked us to wait (from a Retry-After header), if any.
        self.retry_after = retry_after
        # Raw response context, used to compute a precise reset time
        # (X-RateLimit-Reset, retry-after-ms, OpenRouter metadata.headers, etc.).
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body


class PaymentRequiredError(InferenceError):
    """A provider returned 402 - account has insufficient credits.

    ModelDispatcher applies a long cooldown to that (model, provider) pair
    and continues to the next candidate.
    """

    def __init__(
        self,
        model_id: str,
        provider_name: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__(f"Payment required: {model_id} on {provider_name}")
        self.model_id = model_id
        self.provider_name = provider_name
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body


class ProviderAuthError(InferenceError):
    """A provider rejected the request with a hard auth/billing failure.

    ModelDispatcher treats this as a provider-level circuit-breaker event and
    stops routing new models to that provider until the breaker expires.
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
