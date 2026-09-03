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
from inference.exceptions import ModelNotFoundError, PaymentRequiredError, PoolRefreshError
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


# Stand-in output price for a paid Zen model whose price the catalog omits. Only
# ever used to keep the price-ranked pools ordered - never presented as a fact.
_ASSUMED_PAID_COST = 0.002


def _is_free_opencode_model(model_id: str) -> bool:
    """True if model_id is a known free-tier model on OpenCode Zen."""
    lower = model_id.lower()
    return (
        lower.endswith("-free")
        or "-free" in lower
        or "free" in lower
        or any(k in lower for k in ["ox-alpha", "0x-alpha", "oxalpha", "0xalpha", "stealth"])
    )


class OpenCodeZenRouter(OpenAICompatibleProvider):
    """OpenCode Zen provider: free-tier chat completions and tool calls."""

    def __init__(self, api_key: str) -> None:
        super().__init__(name="opencode_zen", base_url=OPENCODE_ZEN_BASE_URL, api_key=api_key)
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    def _raise_cooldown_status(self, response: httpx.Response, model_id: str) -> None:
        body = self._safe_json(response)
        if (
            response.status_code == 401
            and isinstance(body, dict)
            and ("CreditsError" in str(body) or "payment method" in str(body).lower())
        ):
            # OpenCode Zen returns 401 with CreditsError when account has no payment card
            raise PaymentRequiredError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=body,
            )
        if response.status_code in (400, 503) and isinstance(body, dict):
            msg = str(body)
            if "Model is unavailable" in msg or "Endpoint is unavailable" in msg:
                raise ModelNotFoundError(
                    model_id,
                    self.name,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=body,
                )
        if response.status_code in (401, 403) and not _is_free_opencode_model(model_id):
            raise PaymentRequiredError(
                model_id,
                self.name,
                status_code=response.status_code,
                headers=dict(response.headers),
                body=body,
            )
        super()._raise_cooldown_status(response, model_id)

    @staticmethod
    def _price_of(model: dict, model_id: str) -> tuple[float, bool]:
        """This model's output price, and whether Zen actually published one.

        Zen's catalog is ``{id, object, created, owned_by}`` for most entries, so
        a paid model usually carries no price at all. The stand-in keeps the
        price-ranked pools working, but it is flagged as a guess: presenting it
        as a fact once put a $2,000/Mtok phantom near the top of a chain.
        """
        free = _is_free_opencode_model(model_id)
        pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
        if pricing:
            try:
                return max(float(pricing.get("completion", 0) or 0), float(pricing.get("prompt", 0) or 0)), True
            except (TypeError, ValueError):
                pass
        elif "cost" in model:
            try:
                return float(model["cost"]), True
            except (TypeError, ValueError):
                pass
        # Free is a fact Zen states through the model id; a paid price is not.
        return (0.0, True) if free else (_ASSUMED_PAID_COST, False)

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
            cost, price_known = self._price_of(m, model_id)
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name="opencode_zen",
                capabilities=caps,
                context_window=ctx,
                cost_per_token=cost,
                base_quality=quality_from_cost(cost),
                price_known=price_known,
            )

        if live:
            self._models = live
