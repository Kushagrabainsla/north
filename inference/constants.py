"""Inference module constants."""
from __future__ import annotations

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

DEFAULT_TIMEOUT_SECONDS = 60.0
# Max seconds to wait between consecutive SSE chunks before declaring a stall.
SSE_CHUNK_TIMEOUT_SECONDS = 30.0

# Quality scoring constants (Phase 1: price-based proxy).
# Phase 2: replace with hybrid price + confidence tracker score.
_QUALITY_LOG_MIN = -6.0   # log10 of ~$0.000001/token floor
_QUALITY_LOG_MAX = -1.82  # log10 of ~$0.015/token ceiling (frontier model)
_FREE_MODEL_QUALITY = 0.35  # floor for free-tier models (cost_per_token == 0)
