"""OpenCode Zen inference provider.

Serves completions and tool calls via OpenCode's OpenAI-compatible endpoint
at opencode.ai/zen/v1. The model list is populated from GET /models on each
refresh(). get_models() returns an empty dict until the first refresh()
completes.

Free-tier models include kimi-k2.5-free, glm-5-free, minimax-m2.5-free, etc.
"""

from __future__ import annotations

import logging

import httpx

from inference.capability import ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.constants import OPENCODE_ZEN_BASE_URL
from inference.exceptions import PaymentRequiredError, PoolRefreshError
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


def _is_free_opencode_model(model_id: str) -> bool:
    """True if model_id is a known free-tier model on OpenCode Zen."""
    return model_id.endswith("-free") or "-free" in model_id


class OpenCodeZenRouter(OpenAICompatibleProvider):
    """OpenCode Zen provider: free-tier chat completions and tool calls."""

    def __init__(self, api_key: str) -> None:
        super().__init__(name="opencode_zen", base_url=OPENCODE_ZEN_BASE_URL, api_key=api_key)
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    def _raise_cooldown_status(self, response: httpx.Response, model_id: str) -> None:
        if response.status_code in (401, 403) and not _is_free_opencode_model(model_id):
            raise PaymentRequiredError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=self._safe_json(response),
            )
        super()._raise_cooldown_status(response, model_id)

    async def refresh(self) -> None:
        """Fetch the live model list from OpenCode Zen."""
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
        except httpx.RequestError as e:
            raise PoolRefreshError(f"OpenCode Zen /models request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(f"OpenCode Zen /models returned {e.response.status_code}") from e

        try:
            data = resp.json().get("data", [])
        except ValueError as e:
            raise PoolRefreshError("OpenCode Zen /models response was not JSON") from e

        live: dict[str, ModelInfo] = {}
        for m in data:
            model_id = m.get("id")
            if not isinstance(model_id, str):
                continue
            caps = capabilities_from_model_id(model_id)
            ctx = int(m.get("context_window") or 128_000)
            is_free = _is_free_opencode_model(model_id)
            cost = 0.0 if is_free else 0.002
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name="opencode_zen",
                capabilities=caps,
                context_window=ctx,
                cost_per_token=cost,
                base_quality=quality_from_cost(cost),
            )

        if live:
            self._models = live
