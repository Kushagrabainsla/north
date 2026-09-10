"""Runtime inference-router construction shared by composition and interfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.settings import settings
from config.strategy import NorthSettings
from inference import InferenceRouter
from inference.factory import build_router


def build_inference_router_from_settings(
    *, north_settings: NorthSettings, confidence_tracker: Any, models_db_path: Path | None = None
) -> InferenceRouter:
    """Build a router from live settings; reloads intentionally omit models DB."""
    return build_router(
        openrouter_api_key=settings.openrouter_api_key,
        north_settings=north_settings,
        groq_api_key=settings.groq_api_key,
        gemini_api_key=settings.gemini_api_key,
        opencode_zen_api_key=settings.opencode_zen_api_key,
        provider_settings=settings,
        confidence_tracker=confidence_tracker,
        cooldowns_path=settings.north_home / "cooldowns.json",
        models_db_path=models_db_path,
    )


def rebuild_runtime_router(deps: Any) -> InferenceRouter:
    """Build the replacement live router after settings have been reloaded."""
    return build_inference_router_from_settings(
        north_settings=deps.north_settings,
        confidence_tracker=deps.confidence_tracker,
    )
