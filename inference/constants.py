"""Inference module constants."""

from __future__ import annotations

# Provider base URLs
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# HTTP timeouts
DEFAULT_TIMEOUT_SECONDS = 60.0
SSE_CHUNK_TIMEOUT_SECONDS = 30.0  # max seconds between SSE chunks before declaring a stall

# Price-based quality normalisation for base_quality.
_QUALITY_LOG_MIN = -6.0    # log10 of ~$0.000001/token floor
_QUALITY_LOG_MAX = -1.82   # log10 of ~$0.015/token ceiling (frontier model)
_FREE_MODEL_QUALITY = 0.35  # floor for free-tier models (cost_per_token == 0)

# Pool tier thresholds used by ModelDispatcher.current_pools().
_QUALITY_TIER_HIGH: float = 0.70
_QUALITY_TIER_MEDIUM: float = 0.40

# Per-model EMA confidence blended with base_quality for candidate ranking.
_DEFAULT_MODEL_CONFIDENCE: float = 0.5
_MODEL_CONFIDENCE_ALPHA: float = 0.15
_MODEL_CONFIDENCE_MAX_WEIGHT: float = 0.30
_MODEL_CONFIDENCE_FULL_USES: int = 20
