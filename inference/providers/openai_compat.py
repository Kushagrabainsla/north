"""Generic HTTP client for any provider that speaks the OpenAI wire format.

Subclasses set self.name, call super().__init__(), then optionally override
embed() or transcribe() for providers that support those capabilities.
All methods accept an explicit model_id — model selection belongs to ModelDispatcher.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import httpx

from inference.constants import DEFAULT_TIMEOUT_SECONDS, SSE_CHUNK_TIMEOUT_SECONDS
from inference.exceptions import (
    InferenceError,
    ModelRateLimitedError,
    PaymentRequiredError,
    TranscriptionError,
)
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)

logger = logging.getLogger(__name__)


async def _aiter_with_chunk_timeout(aiter, timeout: float):
    """Wrap an async iterator, raising InferenceError if a chunk takes too long."""
    while True:
        try:
            yield await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise InferenceError(
                f"SSE stream stalled for {timeout:.0f}s — model stopped generating"
            ) from exc


class OpenAICompatibleProvider:
    """Base class for providers that use the OpenAI wire format over HTTPS.

    Handles all HTTP mechanics. Subclasses supply provider-specific details:
    name, base_url, api_key, and optional overrides for embed/transcribe.
    """

    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # ---- status helpers ----

    def _raise_for_status(self, response: httpx.Response, model_id: str) -> None:
        if response.status_code == 402:
            raise PaymentRequiredError(
                f"{self.name} returned 402 — insufficient credits"
            )
        if response.status_code in (429, 404, 503):
            raise ModelRateLimitedError(model_id, self.name)
        if response.status_code >= 400:
            raise InferenceError(
                f"{self.name} returned {response.status_code} for {model_id}: "
                f"{response.text[:200]}"
            )

    async def _raise_for_stream_status(
        self, resp: httpx.Response, model_id: str
    ) -> None:
        if resp.status_code == 402:
            await resp.aread()
            raise PaymentRequiredError(
                f"{self.name} returned 402 — insufficient credits"
            )
        if resp.status_code in (429, 404, 503):
            await resp.aread()
            raise ModelRateLimitedError(model_id, self.name)
        if resp.status_code >= 400:
            body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
            raise InferenceError(
                f"{self.name} returned {resp.status_code} for {model_id}: {body}"
            )

    async def aclose(self) -> None:
        """Close the underlying HTTPX client."""
        await self._client.aclose()

    def _extra_body_fields(self) -> dict:
        """Provider-specific fields to merge into every request body.

        Override in subclasses that require non-standard fields.
        Example: OpenRouterProvider adds {"usage": {"include": True}}.
        """
        return {}

    # ---- completion ----

    async def complete(
        self, model_id: str, request: CompletionRequest
    ) -> CompletionResponse:
        body: dict = {
            "model": model_id,
            "messages": [{"role": "user", "content": request.prompt}],
            **self._extra_body_fields(),
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
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

        self._raise_for_status(response, model_id)

        try:
            payload = response.json()
        except ValueError as e:
            raise InferenceError(f"{self.name} response was not JSON") from e

        choices = payload.get("choices") or []
        if not choices:
            raise InferenceError(
                f"{self.name} returned empty choices for {model_id}: {payload}"
            )
        content = choices[0].get("message", {}).get("content") or ""
        usage = payload.get("usage", {})
        return CompletionResponse(
            text=content,
            model_used=payload.get("model", model_id),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost", 0.0)),
        )

    # ---- tool calls ----

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        body: dict = {
            "model": model_id,
            "messages": request.messages,
            "tools": request.tools,
            "stream": True,
            **self._extra_body_fields(),
        }
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0

        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=body
            ) as resp:
                await self._raise_for_stream_status(resp, model_id)
                async for raw_line in _aiter_with_chunk_timeout(
                    resp.aiter_lines(), SSE_CHUNK_TIMEOUT_SECONDS
                ):
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
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "", "name": "", "arguments": ""
                            }
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_calls_acc[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls_acc[idx]["arguments"] += fn["arguments"]
        except httpx.RequestError as e:
            raise InferenceError(f"Request to {self.name} failed: {e}") from e

        if tool_calls_acc:
            calls = [
                ToolCall(
                    name=tc["name"],
                    call_id=tc["id"] or f"call_{tc['name']}_{idx}",
                    params=self._parse_json_args(tc["arguments"]),
                )
                for idx, tc in sorted(tool_calls_acc.items())
            ]
            return ToolCallResponse(
                type="tool_calls",
                calls=calls,
                content=None,
                model_used=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
            )

        return ToolCallResponse(
            type="message",
            content="".join(content_parts),
            calls=[],
            model_used=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

    @staticmethod
    def _parse_json_args(raw: str) -> dict:
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    # ---- embeddings (override in providers that support it) ----

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        raise InferenceError(f"{self.name} does not support embeddings")

    # ---- transcription (override in providers that support it) ----

    async def transcribe(
        self, model_id: str, request: TranscriptionRequest
    ) -> TranscriptionResponse:
        raise TranscriptionError(f"{self.name} does not support transcription")

    # ---- lifecycle ----

    async def aclose(self) -> None:
        await self._client.aclose()
