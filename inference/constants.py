"""Inference module constants."""
from __future__ import annotations

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_EMBED_MODEL = "openai/text-embedding-3-small"
# Max seconds to wait between consecutive SSE chunks before declaring a stall.
SSE_CHUNK_TIMEOUT_SECONDS = 30.0
