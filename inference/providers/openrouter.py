"""OpenRouter inference provider.

Fetches the live model catalogue from GET /api/v1/models and serves as the
broadest fallback provider. get_models() returns an empty dict until the
first refresh() completes - the startup lifespan guarantees that happens
before the server accepts requests.
"""

from __future__ import annotations

import logging

import httpx

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.constants import OPENROUTER_BASE_URL
from inference.exceptions import (
    InferenceError,
    ModelRateLimitedError,
    PoolRefreshError,
    TranscriptionError,
)
from inference.models import (
    EmbedRequest,
    EmbedResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


def _capabilities_from_api(model_id: str, model: dict) -> frozenset[ModelCapability]:
    """Extract capability flags from OpenRouter's model record.

    Name-based inference takes precedence for embedding and transcription models
    whose supported_parameters field is absent or misleading. For chat models,
    supported_parameters determines whether tool calls are available.
    """
    name_caps = capabilities_from_model_id(model_id)
    if ModelCapability.EMBEDDING in name_caps or ModelCapability.TRANSCRIPTION in name_caps:
        return name_caps
    params = set(model.get("supported_parameters") or [])
    caps = {ModelCapability.COMPLETION}
    if "tools" in params or "tool_choice" in params:
        caps.add(ModelCapability.TOOL_CALLS)
    return frozenset(caps)


def _cost_from_api(model: dict) -> float:
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("completion", 0))
    except (TypeError, ValueError):
        return 0.0


class OpenRouterRouter(OpenAICompatibleProvider):
    """OpenRouter provider: dynamic model catalogue, embedding, and transcription.

    Model list is populated exclusively from GET /api/v1/models on each
    refresh(). get_models() returns an empty dict until the first refresh()
    completes.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            name="openrouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
        )
        if client is not None:
            self._client = client
        self._models: dict[str, ModelInfo] = {}

    # ---- Provider protocol ----

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Fetch the live model catalogue from OpenRouter and replace self._models."""
        try:
            response = await self._client.get("/models")
            response.raise_for_status()
        except httpx.RequestError as e:
            raise PoolRefreshError(f"OpenRouter request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(f"OpenRouter returned {e.response.status_code}") from e

        try:
            payload = response.json()
            raw_models: list[dict] = payload.get("data", payload)
        except ValueError as e:
            raise PoolRefreshError("OpenRouter response was not JSON") from e

        self._models = self._build_model_info(raw_models)

    # ---- OpenRouter-specific request body ----

    def _extra_body_fields(self) -> dict:
        return {"usage": {"include": True}}

    # ---- Embeddings ----

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        body = {"model": model_id, "input": request.texts}
        try:
            resp = await self._client.post("/embeddings", json=body)
        except httpx.RequestError as e:
            raise InferenceError(f"Embedding request to openrouter failed: {e}") from e
        self._raise_for_status(resp, model_id)
        try:
            payload = resp.json()
        except ValueError as e:
            raise InferenceError("OpenRouter embeddings response was not JSON") from e

        data = payload.get("data", [])
        data.sort(key=lambda d: d.get("index", 0))
        usage = payload.get("usage", {})
        return EmbedResponse(
            embeddings=[d["embedding"] for d in data],
            model_used=payload.get("model", model_id),
            cost_usd=float(usage.get("cost", 0.0)),
        )

    # ---- Transcription ----

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        files = {"file": ("audio.wav", request.audio, "audio/wav")}
        data = {"model": model_id}
        try:
            response = await self._client.post("/audio/transcriptions", files=files, data=data)
        except httpx.RequestError as e:
            raise TranscriptionError(f"Transcription request to openrouter failed: {e}") from e
        if response.status_code in (429, 503):
            raise ModelRateLimitedError(model_id, self.name)
        if response.status_code >= 400:
            raise TranscriptionError(f"OpenRouter returned {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError as e:
            raise TranscriptionError("OpenRouter transcription response was not JSON") from e

        usage = payload.get("usage", {})
        return TranscriptionResponse(
            text=payload.get("text", ""),
            model_used=payload.get("model", model_id),
            cost_usd=float(usage.get("cost", 0.0)),
        )

    # ---- Internal helpers ----

    def _build_model_info(self, raw_models: list[dict]) -> dict[str, ModelInfo]:
        """Build a ModelInfo dict from a live /models API response."""
        result: dict[str, ModelInfo] = {}
        for m in raw_models:
            model_id = m.get("id")
            if not isinstance(model_id, str):
                continue
            cost = _cost_from_api(m)
            caps = _capabilities_from_api(model_id, m)
            ctx = 0 if ModelCapability.TRANSCRIPTION in caps else int(m.get("context_length") or 128_000)
            result[model_id] = ModelInfo(
                model_id=model_id,
                provider_name="openrouter",
                capabilities=caps,
                context_window=ctx,
                cost_per_token=cost,
                base_quality=quality_from_cost(cost),
            )
        return result
