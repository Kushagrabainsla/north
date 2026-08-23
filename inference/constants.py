"""Inference module constants."""

from __future__ import annotations

# Provider base URLs
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"

# HTTP timeouts
DEFAULT_TIMEOUT_SECONDS = 60.0
SSE_CHUNK_TIMEOUT_SECONDS = 30.0  # max seconds between SSE chunks before declaring a stall

# Price-based quality normalisation for base_quality.
_QUALITY_LOG_MIN = -6.0  # log10 of ~$0.000001/token floor
_QUALITY_LOG_MAX = -1.82  # log10 of ~$0.015/token ceiling (frontier model)
_FREE_MODEL_QUALITY = 0.35  # floor for free-tier models (cost_per_token == 0)

# Pool tier thresholds used by ModelDispatcher.current_pools().
_QUALITY_TIER_HIGH: float = 0.70
_QUALITY_TIER_MEDIUM: float = 0.40

# Per-model EMA confidence blended with base_quality for candidate ranking.
_DEFAULT_MODEL_CONFIDENCE: float = 0.5
_MODEL_CONFIDENCE_ALPHA: float = 0.15
_MODEL_CONFIDENCE_MAX_WEIGHT: float = 0.30
_MODEL_CONFIDENCE_FULL_USES: int = 20

# Preferred-model promotion (see inference/model_policy.py). A curated model is
# only promoted ahead of the price-ranked catalog while it is healthy: once it
# has been tried at least _PREFERRED_MIN_USES times and its success EMA has
# fallen below _PREFERRED_HEALTH_FLOOR, it drops back to its normal price
# position instead of being retried first on every call (so a persistently
# failing preference cannot pin latency/errors to the front of the queue).
_PREFERRED_MIN_USES: int = 5
_PREFERRED_HEALTH_FLOOR: float = 0.35

# Model family-quality tiers (price-FREE prior). Keyed by case-insensitive
# substring; longest match wins. This is the static "smartness prior" used when
# price carries no signal (all providers free). Overridable per-install via
# ~/.north/model_tiers.json (see inference/model_scorer.py).
MODEL_FAMILY_TIERS: dict[str, float] = {
    # Frontier reasoning / pro
    "claude-opus-4": 0.96,
    "claude-opus": 0.95,
    "gpt-5": 0.95,
    "gemini-3.1-pro": 0.94,
    "gemini-3-pro": 0.93,
    "gemini-2.5-pro": 0.92,
    "ox-alpha": 0.88,
    "0x-alpha": 0.88,
    "0xalpha": 0.88,
    "stealth": 0.88,
    "claude-sonnet": 0.85,
    "gemini-2.0-pro": 0.80,
    "gpt-oss-120b": 0.80,
    "deepseek-r1": 0.78,
    "gemini-pro": 0.78,   # generic pro catch-all
    "claude": 0.75,        # generic claude catch-all
    # Mid
    "llama-3.3-70b": 0.62,
    "gemini-3.1-flash": 0.72,
    "gemini-3-flash": 0.72,
    "gemini-2.5-flash": 0.70,
    "gemini-flash": 0.65,  # generic flash catch-all
    "qwen3.6-27b": 0.60,
    "qwen3-32b": 0.58,
    "deepseek": 0.60,
    "gpt": 0.60,           # generic gpt catch-all
    # Small / fast
    "llama-3.1-8b": 0.32,
    "llama-3-8b": 0.30,
    "gpt-oss-20b": 0.42,
    "allam": 0.25,
    "llama": 0.40,
    "gemini": 0.50,        # generic gemini catch-all (floor for unmatched gemini)
}

# (task_id, component, capability, priority) is reused for that task's later
# steps so a multi-step coding task is done by one consistent model. Bounded
# LRU so a long-lived server never grows this map without limit.
_STICKY_MAX_ENTRIES: int = 512
