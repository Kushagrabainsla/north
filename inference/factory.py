"""Build the active router from the shared provider registry."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from config.strategy import NorthSettings
from inference.base import InferenceRouter
from inference.dispatcher import ModelDispatcher
from inference.provider import Provider
from inference.registry import PROVIDER_DEFINITIONS, AuthKind

if TYPE_CHECKING:
    from tools.confidence import ConfidenceTracker


def build_router(
    *,
    openrouter_api_key: str,
    north_settings: NorthSettings | None = None,
    groq_api_key: str = "",
    gemini_api_key: str = "",
    opencode_zen_api_key: str = "",
    confidence_tracker: ConfidenceTracker | None = None,
    cooldowns_path: Path | None = None,
    provider_settings: object | None = None,
) -> InferenceRouter:
    """Assemble a ModelDispatcher from configured provider definitions.

    Providers are ordered by their registry fallback order. The named key
    arguments remain as a compatibility layer for existing callers.
    """
    providers: list[Provider] = []
    credentials = {
        "openrouter_api_key": openrouter_api_key,
        "groq_api_key": groq_api_key,
        "gemini_api_key": gemini_api_key,
        "opencode_zen_api_key": opencode_zen_api_key,
    }
    for definition in sorted(PROVIDER_DEFINITIONS, key=lambda item: item.fallback_order):
        if definition.auth_kind is AuthKind.API_KEY:
            credential = credentials.get(definition.settings_field or "", "")
            if not credential and provider_settings is not None:
                credential = definition.resolve_credential(provider_settings)
            if credential:
                providers.append(definition.build(credential))
        elif definition.is_configured(provider_settings):
            providers.append(definition.build())

    return ModelDispatcher(providers, north_settings, confidence_tracker, cooldowns_path)
