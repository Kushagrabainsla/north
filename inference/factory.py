"""Builds the active InferenceRouter from current settings.

Add new provider branches here when a new provider key is introduced.
OpenRouter is always the last provider in the chain — broadest model
coverage as the final fallback.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from config.strategy import NorthSettings
from inference.base import InferenceRouter
from inference.dispatcher import ModelDispatcher
from inference.provider import Provider

if TYPE_CHECKING:
    from tools.confidence import ConfidenceTracker


def build_router(
    *,
    openrouter_api_key: str,
    north_settings: NorthSettings | None = None,
    groq_api_key: str = "",
    gemini_api_key: str = "",
    confidence_tracker: ConfidenceTracker | None = None,
) -> InferenceRouter:
    """Assemble a ModelDispatcher from available provider keys.

    Direct providers (Groq, Gemini) are prepended when their keys are present
    so they are preferred over OpenRouter for their own models.  OpenRouter
    is always included as the broadest fallback.
    """
    providers: list[Provider] = []

    if groq_api_key:
        from inference.providers.groq import GroqRouter
        providers.append(GroqRouter(groq_api_key))

    if gemini_api_key:
        from inference.providers.gemini import GeminiRouter
        providers.append(GeminiRouter(gemini_api_key))

    from inference.providers.openrouter import OpenRouterRouter
    providers.append(OpenRouterRouter(openrouter_api_key))

    return ModelDispatcher(providers, north_settings, confidence_tracker)
