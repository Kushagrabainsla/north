"""Hardcoded minimal pools used if OpenRouter is unreachable on startup AND
no `inference_cache.json` exists yet. See README Section 8.2.

Refreshing live pools is the happy path; these values are a safety net so
the system continues accepting tasks even with no network.
"""

from __future__ import annotations

from inference.models import ModelPool

FALLBACK_POOLS: dict[str, ModelPool] = {
    "reasoning": ModelPool(
        name="reasoning",
        models=[
            "anthropic/claude-opus-4-7",
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o",
        ],
    ),
    "fast_cheap": ModelPool(
        name="fast_cheap",
        models=[
            "anthropic/claude-haiku-4-5",
            "openai/gpt-4o-mini",
            "google/gemini-flash",
        ],
    ),
    "high_volume": ModelPool(
        name="high_volume",
        models=[
            "openai/gpt-4o-mini",
            "google/gemini-flash",
            "anthropic/claude-haiku-4-5",
        ],
    ),
    "free_fallback": ModelPool(
        name="free_fallback",
        models=[
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-8b:free",
            "mistralai/mistral-7b-instruct:free",
        ],
    ),
}

DEFAULT_TRANSCRIPTION_MODEL = "groq/whisper-large-v3"
