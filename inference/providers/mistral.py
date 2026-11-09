"""Mistral's API: Mistral's own models, and the open-weight models it hosts, such as Z.ai's GLM and DeepSeek.

One key from console.mistral.ai reaches all of them, and a plan's monthly allowance is used before
pay-as-you-go. The model list (``GET /models``) names a model once per alias (``zai-glm-5-3``,
``zai-glm-5``, ``zai-glm-latest``), says what each can do, and carries no price. So north lists each
model once, under the name its aliases share; takes what it can do from Mistral's own flags; and
prices each call from Mistral's published rates, each kind of token at its own rate, since the API
reports no cost. A reasoning model's answer arrives as a list of parts, which the OpenAI-compatible
base reads.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from inference.capability import ModelCapability, ModelInfo, capabilities_from_model_id, quality_from_cost
from inference.constants import MISTRAL_BASE_URL
from inference.exceptions import PoolRefreshError
from inference.models import CompletionRequest, CompletionResponse, ToolCallRequest, ToolCallResponse
from inference.providers.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

NAME = "mistral"


@dataclass(frozen=True)
class Rates:
    """USD per million tokens. A cached-input rate Mistral does not publish is charged as input."""

    input: float
    output: float
    cached: float | None = None


# Mistral's published API prices, by billing name (https://mistral.ai/pricing/api/, November 2026).
# A model not here is still listed, at a stand-in price for ranking, and its calls are recorded as
# unpriced: never as free, and never at a figure nobody published.
PRICES: dict[str, Rates] = {
    "zai-glm-5-3": Rates(1.40, 4.40, cached=0.14),
    "zai-glm-5-2": Rates(1.40, 4.40, cached=0.14),
    "zai-glm-5-3-flash": Rates(0.15, 0.50),
    "deepseek-v4-1-flash-1017": Rates(0.30, 1.20, cached=0.007),
    "mistral-large-4-0-a": Rates(0.70, 2.10, cached=0.07),
    "mistral-medium-3-5": Rates(1.50, 7.50),
    "mistral-small-2603": Rates(0.15, 0.60, cached=0.015),
    "ministral-14b-2512": Rates(0.20, 0.20, cached=0.02),
    "ministral-8b-2512": Rates(0.15, 0.15, cached=0.015),
    "ministral-3b-2512": Rates(0.10, 0.10, cached=0.01),
    "codestral-2508": Rates(0.30, 0.90, cached=0.03),
}
# The stand-in an unpriced model is ranked by: the middle of the table's output prices.
_STAND_IN_OUTPUT = sorted(rates.output for rates in PRICES.values())[len(PRICES) // 2]

# Not for north's work, though Mistral lists them as chat models: Voxtral is for audio, and
# Mistral Labs' models are experiments (Leanstral writes Lean proofs).
_NOT_FOR_TASKS = ("voxtral", "labs-")
_FLAGS = {
    "completion_chat": ModelCapability.COMPLETION,
    "function_calling": ModelCapability.TOOL_CALLS,
    "vision": ModelCapability.VISION,
    "reasoning": ModelCapability.REASONING,
}

R = TypeVar("R", CompletionResponse, ToolCallResponse)


def cost_usd(rates: Rates, tokens_in: int, cached_tokens: int, tokens_out: int) -> float:
    """What one call cost: uncached input, cached input and output, each at its own rate."""
    cached = min(cached_tokens, tokens_in)
    cached_rate = rates.cached if rates.cached is not None else rates.input
    per_million = (tokens_in - cached) * rates.input + cached * cached_rate + tokens_out * rates.output
    return per_million / 1_000_000


class MistralRouter(OpenAICompatibleProvider):
    """Mistral provider: Mistral's models and the open-weight models it hosts (GLM, DeepSeek)."""

    def __init__(self, api_key: str) -> None:
        super().__init__(name=NAME, base_url=MISTRAL_BASE_URL, api_key=api_key)
        self._models: dict[str, ModelInfo] = {}
        self._billing: dict[str, str] = {}  # every id and alias Mistral listed -> its billing name

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Replace the model list with what this key can call now, each model once."""
        try:
            response = await self._client.get("/models")
            response.raise_for_status()
            listed = response.json().get("data", [])
        except httpx.RequestError as e:
            raise PoolRefreshError(f"Mistral /models request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(f"Mistral /models returned {e.response.status_code}") from e
        except ValueError as e:
            raise PoolRefreshError("Mistral /models response was not JSON") from e
        models, billing = _catalog(listed)
        if models:
            self._models, self._billing = models, billing

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        return self._priced(model_id, await super().complete(model_id, request))

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        return self._priced(model_id, await super().complete_with_tools(model_id, request, token_callback))

    def _priced(self, model_id: str, response: R) -> R:
        """*response* with its cost from Mistral's rates, or marked unpriced when Mistral published none."""
        rates = PRICES.get(self._billing.get(model_id, model_id))
        if rates is None:
            return response.model_copy(update={"cost_usd": 0.0, "priced": False})
        cost = cost_usd(rates, response.tokens_in, response.cached_tokens, response.tokens_out)
        return response.model_copy(update={"cost_usd": cost, "priced": True})


def _catalog(listed: list[Any]) -> tuple[dict[str, ModelInfo], dict[str, str]]:
    """The models to offer, one per name its aliases share, and the billing name of every listed id."""
    models: dict[str, ModelInfo] = {}
    billing: dict[str, str] = {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        billing[entry["id"]] = str(entry.get("billing_model_name") or entry["id"])
        flags = entry.get("capabilities") or {}
        if not flags.get("completion_chat") or any(word in entry["id"] for word in _NOT_FOR_TASKS):
            continue
        groups.setdefault(str(entry.get("name") or entry["id"]), []).append(entry)
    for name, entries in groups.items():
        entry = next((item for item in entries if item["id"] == name), entries[0])
        models[entry["id"]] = _model(entry, billing[entry["id"]])
    return models, billing


def _model(entry: dict[str, Any], billing_name: str) -> ModelInfo:
    flags = entry.get("capabilities") or {}
    capabilities = {capability for flag, capability in _FLAGS.items() if flags.get(flag)}
    # Mistral says nothing of speed, and flags reasoning only on models that think aloud:
    # the name tells the rest, as it does for every other provider.
    named = capabilities_from_model_id(entry["id"], NAME)
    capabilities |= named & {ModelCapability.SPEED, ModelCapability.REASONING}
    rates = PRICES.get(billing_name)
    output = (rates.output if rates else _STAND_IN_OUTPUT) / 1_000_000
    return ModelInfo(
        model_id=entry["id"],
        provider_name=NAME,
        capabilities=frozenset(capabilities),
        context_window=int(entry.get("max_context_length") or 32_768),
        cost_per_token=output,
        base_quality=quality_from_cost(output),
        price_known=rates is not None,
    )
