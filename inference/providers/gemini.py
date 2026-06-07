"""Gemini inference provider.

Serves completions, tool calls, and embeddings via Google's OpenAI-compatible
endpoint. The model list is populated exclusively from GET /models on each
refresh(); capabilities are inferred from model ID naming conventions.
get_models() returns an empty dict until the first refresh() completes.
"""
from __future__ import annotations

import logging

import httpx

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.constants import GEMINI_BASE_URL
from inference.exceptions import InferenceError, PoolRefreshError
from inference.models import EmbedRequest, EmbedResponse
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

# Gemini's OpenAI-compatible /models endpoint does not return context_window.
# 1M is used as the assumed value for all Gemini generative models.
_DEFAULT_CONTEXT_WINDOW = 1_048_576


class GeminiRouter(OpenAICompatibleProvider):
    """Gemini provider: free-tier chat completions, tool calls, and embeddings."""

    def __init__(self, api_key: str) -> None:
        super().__init__(name="gemini", base_url=GEMINI_BASE_URL, api_key=api_key)
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Fetch the live model list from Gemini and replace self._models."""
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
        except httpx.RequestError as e:
            raise PoolRefreshError(f"Gemini /models request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(f"Gemini /models returned {e.response.status_code}") from e

        try:
            data = resp.json().get("data", [])
        except ValueError as e:
            raise PoolRefreshError("Gemini /models response was not JSON") from e

        live: dict[str, ModelInfo] = {}
        for m in data:
            model_id = m.get("id")
            if not isinstance(model_id, str):
                continue
            caps = capabilities_from_model_id(model_id)
            ctx = 0 if ModelCapability.TRANSCRIPTION in caps else int(m.get("context_window") or _DEFAULT_CONTEXT_WINDOW)
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name="gemini",
                capabilities=caps,
                context_window=ctx,
                cost_per_token=0.0,
                base_quality=quality_from_cost(0.0),
            )

        if live:
            self._models = live

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        body = {"model": model_id, "input": request.texts}
        try:
            resp = await self._client.post("/embeddings", json=body)
        except httpx.RequestError as e:
            raise InferenceError(f"Embedding request to Gemini failed: {e}") from e
        self._raise_for_status(resp, model_id)
        try:
            payload = resp.json()
        except ValueError as e:
            raise InferenceError("Gemini embeddings response was not JSON") from e
        data = payload.get("data", [])
        data.sort(key=lambda d: d.get("index", 0))
        return EmbedResponse(
            embeddings=[d["embedding"] for d in data],
            model_used=payload.get("model", model_id),
            cost_usd=0.0,
        )
