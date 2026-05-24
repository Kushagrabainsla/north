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
from config.strategy import NorthSettings, StrategyMode
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
        north_settings: NorthSettings | None = None,
    ) -> None:
        self._api_key = api_key
        self._cache_path = cache_path
        self._client = client or httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._default_transcription_model = default_transcription_model
        self._north_settings = north_settings
        self._pools: dict[str, ModelPool] = {}
        self._all_models_asc: list[str] = []  # all priced models, cheapest first
        self._load_initial_pools()

    # ---- pool state ----

    def _load_initial_pools(self) -> None:
        if self._cache_path.exists():
            try:
                raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
                self._pools = {name: ModelPool(**pool) for name, pool in raw.items()}
                self._all_models_asc = _models_asc_from_pools(self._pools)
                return
            except (OSError, ValueError):
                pass
        self._pools = dict(FALLBACK_POOLS)
        self._all_models_asc = _models_asc_from_pools(self._pools)

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

        self._pools, self._all_models_asc = _bucket_models(models)
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
        strategy = (
            self._north_settings.strategy
            if self._north_settings is not None
            else StrategyMode.CRUISE
        )
        chain = self._build_chain(request.priority, strategy)

        last_error: Exception | None = None
        for model in chain:
            try:
                return await self._call_completion(model, request)
            except _RateLimited as e:
                last_error = e
                continue

        raise AllModelsRateLimitedError(
            f"All models exhausted (strategy={strategy.value}, priority={request.priority.value})"
        ) from last_error

    def _build_chain(self, priority: PoolPriority, strategy: StrategyMode) -> list[str]:
        """Return an ordered list of models to try for this (priority, strategy) pair."""
        free = list(self._pools.get("free_fallback", ModelPool(name="f", models=[])).models)

        if strategy == StrategyMode.ECO:
            # Cheapest first across all priced models, free at tail
            return _dedup(self._all_models_asc + free)

        if strategy == StrategyMode.SPORT:
            # Most capable first, free at tail
            return _dedup(list(reversed(self._all_models_asc)) + free)

        # CRUISE: role-aware starting pool, fall through remaining tiers, free last
        pool_order: list[str]
        if priority == PoolPriority.HIGH:
            # reasoning → fast_cheap → high_volume → free
            pool_order = ["reasoning", "fast_cheap", "high_volume"]
        elif priority == PoolPriority.MEDIUM:
            # fast_cheap → high_volume → reasoning → free
            pool_order = ["fast_cheap", "high_volume", "reasoning"]
        else:
            # high_volume → fast_cheap → free
            pool_order = ["high_volume", "fast_cheap", "reasoning"]

        chain: list[str] = []
        for pool_name in pool_order:
            pool = self._pools.get(pool_name)
            if pool:
                chain.extend(pool.models)
        return _dedup(chain + free)

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

        if response.status_code in (429, 402, 404, 503):
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


def _dedup(models: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _models_asc_from_pools(pools: dict[str, ModelPool]) -> list[str]:
    """Build cheapest-first ordered list from pool structure (fallback case)."""
    # high_volume = cheapest tier, fast_cheap = mid, reasoning = most expensive
    asc: list[str] = []
    for name in ("high_volume", "fast_cheap", "reasoning"):
        pool = pools.get(name)
        if pool:
            asc.extend(pool.models)
    return _dedup(asc)


def _bucket_models(models: list[dict]) -> tuple[dict[str, ModelPool], list[str]]:
    """Bucket OpenRouter's `/models` response into pools by output cost.

    Returns (pools, all_priced_asc) where all_priced_asc is every priced model
    sorted cheapest-first — used by eco/sport strategy chains.
    """
    priced: list[tuple[str, float]] = []
    free_ids: list[str] = []

    for m in models:
        model_id = m.get("id")
        if not isinstance(model_id, str):
            continue
        completion_price = _output_price(m)
        if completion_price <= 0:
            if model_id.endswith(":free"):
                free_ids.append(model_id)
        else:
            priced.append((model_id, completion_price))

    # Prefer the static free list as a known-good baseline; extend with live ones.
    static_free = list(FALLBACK_POOLS["free_fallback"].models)
    merged_free = static_free + [m for m in free_ids if m not in static_free]

    if not priced:
        pools = dict(FALLBACK_POOLS)
        return pools, _models_asc_from_pools(pools)

    # Sort descending for pool bucketing (most expensive = reasoning)
    priced.sort(key=lambda pair: pair[1], reverse=True)
    n = len(priced)
    third = max(1, n // 3)

    reasoning_ids = [mid for mid, _ in priced[:third]]
    fast_cheap_ids = [mid for mid, _ in priced[third : 2 * third]] or reasoning_ids
    high_volume_ids = [mid for mid, _ in priced[-third:]]

    # Cheapest-first list for eco/sport: reverse of the descending-sorted priced list
    all_priced_asc = [mid for mid, _ in reversed(priced)]

    pools = {
        "reasoning": ModelPool(name="reasoning", models=reasoning_ids),
        "fast_cheap": ModelPool(name="fast_cheap", models=fast_cheap_ids),
        "high_volume": ModelPool(name="high_volume", models=high_volume_ids),
        "free_fallback": ModelPool(name="free_fallback", models=merged_free),
    }
    return pools, all_priced_asc


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
