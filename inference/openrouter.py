"""OpenRouter-backed implementation of the Inference Router.

Both chat completion and audio transcription go through the same client and
the same `NORTH_OPENROUTER_API_KEY`. Pool membership is refreshed from
`GET /api/v1/models` and persisted to the configured cache path. On rate
limit, the router walks down the pool to the next available model.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from config.strategy import NorthSettings, StrategyMode
from inference.base import InferenceRouter
from inference.constants import DEFAULT_EMBED_MODEL, DEFAULT_TIMEOUT_SECONDS, OPENROUTER_BASE_URL
from inference.exceptions import (
    AllModelsRateLimitedError,
    InferenceError,
    PoolRefreshError,
    TranscriptionError,
)
from inference.fallback_pools import DEFAULT_TRANSCRIPTION_MODEL, FALLBACK_POOLS
from inference.models import (
    POOL_NAMES,
    PRIORITY_TO_POOL,
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    PoolPriority,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)
from inference.pool_builder import bucket_models, dedup, models_asc_from_pools

logger = logging.getLogger(__name__)


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
                self._all_models_asc = models_asc_from_pools(self._pools)
                return
            except (OSError, ValueError):
                pass
        self._pools = dict(FALLBACK_POOLS)
        self._all_models_asc = models_asc_from_pools(self._pools)

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

        self._pools, self._all_models_asc = bucket_models(models)
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
            except InferenceError as e:
                logger.warning("Model '%s' failed, trying next in chain: %s", model, e)
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
            return dedup(self._all_models_asc + free)

        if strategy == StrategyMode.SPORT:
            # Most capable first, free at tail
            return dedup(list(reversed(self._all_models_asc)) + free)

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
        return dedup(chain + free)

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
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}

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

        choices = payload.get("choices") or []
        if not choices:
            raise InferenceError(
                f"OpenRouter returned empty choices for {model}: {payload}"
            )
        content = choices[0].get("message", {}).get("content") or ""
        usage = payload.get("usage", {})
        return CompletionResponse(
            text=content,
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

    # ---- function calling ----

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        strategy = (
            self._north_settings.strategy
            if self._north_settings is not None
            else StrategyMode.CRUISE
        )
        chain = self._build_chain(request.priority, strategy)
        last_error: Exception | None = None
        for model in chain:
            try:
                return await self._call_tools_streaming(model, request, token_callback)
            except _RateLimited as e:
                last_error = e
                continue
            except InferenceError as e:
                logger.warning("Model '%s' failed, trying next in chain: %s", model, e)
                last_error = e
                continue
        raise AllModelsRateLimitedError(
            f"All models exhausted for tool call (strategy={strategy.value})"
        ) from last_error

    async def _call_tools_streaming(
        self,
        model: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None,
    ) -> ToolCallResponse:
        body: dict = {
            "model": model,
            "messages": request.messages,
            "tools": request.tools,
            "stream": True,
            "usage": {"include": True},
        }
        content_parts: list[str] = []
        # tool_calls[index] = {id, name, arguments_so_far}
        tool_calls: dict[int, dict] = {}
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0

        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code in (429, 402, 404, 503):
                    await resp.aread()
                    raise _RateLimited(model)
                if resp.status_code >= 400:
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                    raise InferenceError(
                        f"OpenRouter returned {resp.status_code} for {model}: {body_text}"
                    )
                async for raw_line in resp.aiter_lines():
                    if not raw_line.startswith("data: "):
                        continue
                    data = raw_line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        tokens_in = usage.get("prompt_tokens", tokens_in)
                        tokens_out = usage.get("completion_tokens", tokens_out)
                        cost_usd = float(usage.get("cost", cost_usd))
                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    text_token = delta.get("content") or ""
                    if text_token:
                        content_parts.append(text_token)
                        if token_callback is not None:
                            await token_callback(text_token)
                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        if idx not in tool_calls:
                            tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.get("id"):
                            tool_calls[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            # name only appears in the first chunk — assign, don't append
                            tool_calls[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            # arguments are streamed in pieces — accumulate
                            tool_calls[idx]["arguments"] += fn["arguments"]
        except httpx.RequestError as e:
            raise InferenceError(f"Request to OpenRouter failed: {e}") from e

        if tool_calls:
            calls = []
            for idx in sorted(tool_calls):
                tc = tool_calls[idx]
                try:
                    params = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    params = {}
                calls.append(ToolCall(
                    name=tc["name"],
                    call_id=tc["id"] or f"call_{tc['name']}_{idx}",
                    params=params,
                ))
            return ToolCallResponse(
                type="tool_calls",
                calls=calls,
                content=None,
                model_used=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )

        return ToolCallResponse(
            type="message",
            content="".join(content_parts),
            calls=[],
            model_used=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

    # ---- embeddings ----

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        body = {"model": DEFAULT_EMBED_MODEL, "input": request.texts}
        try:
            resp = await self._client.post("/embeddings", json=body)
        except httpx.RequestError as e:
            raise InferenceError(f"Embedding request failed: {e}") from e
        if resp.status_code >= 400:
            raise InferenceError(
                f"OpenRouter /embeddings returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise InferenceError("Embeddings response was not JSON") from e
        data = payload.get("data", [])
        # Sort by index to preserve order regardless of API response ordering.
        data.sort(key=lambda d: d.get("index", 0))
        embeddings = [d["embedding"] for d in data]
        usage = payload.get("usage", {})
        cost = float(usage.get("cost", 0.0))
        return EmbedResponse(
            embeddings=embeddings,
            model_used=payload.get("model", DEFAULT_EMBED_MODEL),
            cost_usd=cost,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Call this in the FastAPI lifespan exit."""
        await self._client.aclose()


class _RateLimited(Exception):
    """Internal marker that one model returned 429. Drives in-pool fallback."""

    def __init__(self, model: str) -> None:
        super().__init__(f"Rate limited: {model}")
        self.model = model


# Silence unused-import warnings: POOL_NAMES is re-exported via the package.
_ = POOL_NAMES
