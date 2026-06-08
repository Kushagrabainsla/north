"""Groq inference provider.

Serves completions, tool calls, and audio transcription via Groq's
OpenAI-compatible endpoint. The model list is populated exclusively from
GET /openai/v1/models on each refresh(); capabilities are inferred from
model ID naming conventions. get_models() returns an empty dict until the
first refresh() completes.
"""

from __future__ import annotations

import logging

import httpx

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.constants import GROQ_BASE_URL
from inference.exceptions import ModelRateLimitedError, PoolRefreshError, TranscriptionError
from inference.models import TranscriptionRequest, TranscriptionResponse
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


class GroqRouter(OpenAICompatibleProvider):
    """Groq provider: fast free-tier chat completions and Whisper transcription."""

    def __init__(self, api_key: str) -> None:
        super().__init__(name="groq", base_url=GROQ_BASE_URL, api_key=api_key)
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Fetch the live model list from Groq and replace self._models."""
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
        except httpx.RequestError as e:
            raise PoolRefreshError(f"Groq /models request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(f"Groq /models returned {e.response.status_code}") from e

        try:
            data = resp.json().get("data", [])
        except ValueError as e:
            raise PoolRefreshError("Groq /models response was not JSON") from e

        live: dict[str, ModelInfo] = {}
        for m in data:
            model_id = m.get("id")
            if not isinstance(model_id, str):
                continue
            caps = capabilities_from_model_id(model_id)
            # Transcription models are not token-based; context_window is not meaningful.
            ctx = 0 if ModelCapability.TRANSCRIPTION in caps else int(m.get("context_window") or 131_072)
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name="groq",
                capabilities=caps,
                context_window=ctx,
                cost_per_token=0.0,
                base_quality=quality_from_cost(0.0),
            )

        if live:
            self._models = live

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        files = {"file": ("audio.wav", request.audio, "audio/wav")}
        data = {"model": model_id}
        try:
            resp = await self._client.post("/audio/transcriptions", files=files, data=data)
        except httpx.RequestError as e:
            raise TranscriptionError(f"Transcription request to Groq failed: {e}") from e
        if resp.status_code in (429, 503):
            raise ModelRateLimitedError(model_id, self.name)
        if resp.status_code >= 400:
            raise TranscriptionError(f"Groq returned {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise TranscriptionError("Groq transcription response was not JSON") from e
        return TranscriptionResponse(
            text=payload.get("text", ""),
            model_used=payload.get("model", model_id),
            cost_usd=0.0,
        )
