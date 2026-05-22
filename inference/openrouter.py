"""OpenRouter-backed implementation of the Inference Router.

Both chat completion and audio transcription go through the same client and
the same `NORTH_OPENROUTER_API_KEY`. Pool membership is refreshed from
`GET /api/v1/models` and persisted to the configured cache path. On rate
limit, the router walks down the pool to the next available model.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from inference.base import InferenceRouter
from inference.exceptions import (
    AllModelsRateLimitedError,
    InferenceError,
    PoolRefreshError,
    TranscriptionError,
)
from inference.fallback_pools import DEFAULT_TRANSCRIPTION_MODEL, FALLBACK_POOLS
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    ModelPool,
    POOL_NAMES,
    PRIORITY_TO_POOL,
    PoolPriority,
    TranscriptionRequest,
    TranscriptionResponse,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0


class OpenRouterInferenceRouter(InferenceRouter):
    """Concrete router talking to OpenRouter over HTTPS.

    `cache_path` is where the last successfully refreshed pool snapshot is
    persisted. On construction, the router loads pools in this order of
    preference: cache file → `FALLBACK_POOLS`. A live refresh must be
    triggered explicitly via `refresh_pools()`.
    """

    def __init__(
        self,
        api_key: str,
        cache_path: Path,
        *,
        client: httpx.AsyncClient | None = None,
        default_transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL,
    ) -> None:
        self._api_key = api_key
        self._cache_path = cache_path
        self._client = client or httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._default_transcription_model = default_transcription_model
        self._pools: dict[str, ModelPool] = self._load_initial_pools()

    # ---- pool state ----

    def _load_initial_pools(self) -> dict[str, ModelPool]:
        if self._cache_path.exists():
            try:
                raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
                return {
                    name: ModelPool(**pool) for name, pool in raw.items()
                }
            except (OSError, ValueError):
                pass
        return dict(FALLBACK_POOLS)

    def current_pools(self) -> dict[str, ModelPool]:
        return dict(self._pools)

    async def get_model(self, priority: PoolPriority) -> str:
        pool_name = PRIORITY_TO_POOL[priority]
        pool = self._pools[pool_name]
        if not pool.models:
            raise InferenceError(f"Pool '{pool_name}' is empty")
        return pool.models[0]

    # ---- pool refresh ----

    async def refresh_pools(self) -> None:
        try:
            response = await self._client.get("/models")
            response.raise_for_status()
        except httpx.RequestError as e:
            raise PoolRefreshError(f"OpenRouter request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PoolRefreshError(
                f"OpenRouter returned {e.response.status_code}"
            ) from e

        try:
            payload = response.json()
            models = payload.get("data", payload)  # OpenRouter wraps in {"data": [...]}
        except ValueError as e:
            raise PoolRefreshError("OpenRouter response was not JSON") from e

        self._pools = _bucket_models(models)
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({n: p.model_dump() for n, p in self._pools.items()}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # Non-fatal: we still have the in-memory pools.
            pass

    # ---- completion ----

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        pool = self._pools[PRIORITY_TO_POOL[request.priority]]
        if not pool.models:
            raise InferenceError(f"Pool '{pool.name}' is empty")

        last_error: Exception | None = None
        for model in pool.models:
            try:
                return await self._call_completion(model, request)
            except _RateLimited as e:
                last_error = e
                continue
        raise AllModelsRateLimitedError(
            f"All {len(pool.models)} models in '{pool.name}' rate-limited"
        ) from last_error

    async def _call_completion(
        self, model: str, request: CompletionRequest
    ) -> CompletionResponse:
        body: dict = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "usage": {"include": True},
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature

        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.RequestError as e:
            raise InferenceError(f"Request to OpenRouter failed: {e}") from e

        if response.status_code == 429:
            raise _RateLimited(model)
        if response.status_code >= 400:
            raise InferenceError(
                f"OpenRouter returned {response.status_code} for {model}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as e:
            raise InferenceError("OpenRouter response was not JSON") from e

        choice = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        return CompletionResponse(
            text=choice,
            model_used=payload.get("model", model),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost", 0.0)),
        )

    # ---- transcription ----

    async def transcribe(
        self, request: TranscriptionRequest
    ) -> TranscriptionResponse:
        model = request.model or self._default_transcription_model
        body = {
            "model": model,
            "audio": base64.b64encode(request.audio).decode("ascii"),
        }

        try:
            response = await self._client.post(
                "/audio/transcriptions", json=body
            )
        except httpx.RequestError as e:
            raise TranscriptionError(f"Request to OpenRouter failed: {e}") from e

        if response.status_code >= 400:
            raise TranscriptionError(
                f"OpenRouter returned {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as e:
            raise TranscriptionError("OpenRouter response was not JSON") from e

        usage = payload.get("usage", {})
        return TranscriptionResponse(
            text=payload.get("text", ""),
            model_used=payload.get("model", model),
            cost_usd=float(usage.get("cost", 0.0)),
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Call this in the FastAPI lifespan exit."""
        await self._client.aclose()


class _RateLimited(Exception):
    """Internal marker that one model returned 429. Drives in-pool fallback."""

    def __init__(self, model: str) -> None:
        super().__init__(f"Rate limited: {model}")
        self.model = model


def _bucket_models(models: list[dict]) -> dict[str, ModelPool]:
    """Bucket OpenRouter's `/models` response into three pools by output cost.

    Heuristic: sort by completion price descending; top third → reasoning,
    middle third → fast_cheap, bottom third → high_volume. Models with zero
    or missing pricing data are skipped — they're typically free preview
    endpoints we should not silently route real traffic through.

    Refined manual policy can replace this later (Section 15 open item).
    """
    priced: list[tuple[str, float]] = []
    for m in models:
        model_id = m.get("id")
        if not isinstance(model_id, str):
            continue
        completion_price = _output_price(m)
        if completion_price <= 0:
            continue
        priced.append((model_id, completion_price))

    if not priced:
        return dict(FALLBACK_POOLS)

    priced.sort(key=lambda pair: pair[1], reverse=True)
    n = len(priced)
    third = max(1, n // 3)

    reasoning_ids = [mid for mid, _ in priced[:third]]
    fast_cheap_ids = [mid for mid, _ in priced[third : 2 * third]] or reasoning_ids
    high_volume_ids = [mid for mid, _ in priced[-third:]]

    return {
        "reasoning": ModelPool(name="reasoning", models=reasoning_ids),
        "fast_cheap": ModelPool(name="fast_cheap", models=fast_cheap_ids),
        "high_volume": ModelPool(name="high_volume", models=high_volume_ids),
    }


def _output_price(model: dict) -> float:
    """Return the per-token completion price as a float, or 0 if unparseable."""
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return 0.0
    raw = pricing.get("completion", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


# Silence unused-import warnings: POOL_NAMES is re-exported via the package.
_ = POOL_NAMES
