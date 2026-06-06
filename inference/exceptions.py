"""Inference-layer exceptions."""

from __future__ import annotations

from exceptions import NorthError


class InferenceError(NorthError):
    """Base class for inference-layer failures."""


class AllModelsRateLimitedError(InferenceError):
    """Every model in the requested pool returned a rate-limit response."""


class PaymentRequiredError(InferenceError):
    """OpenRouter returned 402 Payment Required — account has insufficient credits.

    This is a fatal billing error, not a per-model rate limit. The router should
    surface this immediately without retrying other models.
    """


class PoolRefreshError(InferenceError):
    """Failed to fetch the live model list from OpenRouter."""


class TranscriptionError(InferenceError):
    """Audio transcription failed."""
