"""Experimental OpenAI Codex provider using the Responses wire protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from inference.auth import CredentialProvider
from inference.capability import ModelCapability, ModelInfo
from inference.constants import DEFAULT_TIMEOUT_SECONDS, SSE_CHUNK_TIMEOUT_SECONDS
from inference.exceptions import (
    InferenceError,
    ModelNotFoundError,
    ModelRateLimitedError,
    PoolRefreshError,
    ProviderAuthError,
    ProviderUnavailableError,
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
from inference.usage import cache_tokens
from utils.ids import generate_id

logger = logging.getLogger(__name__)


# The item fields north keeps as a descriptor of one output item.
_ITEM_DESCRIPTOR_KEYS = ("id", "type", "status", "name", "call_id")
_FAILURE_DETAIL_CHARS = 300


async def _aiter_response_events(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield each decoded event of a Responses SSE stream, skipping unparsable lines."""
    events = response.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(events.__anext__(), timeout=SSE_CHUNK_TIMEOUT_SECONDS)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise InferenceError("OpenAI Codex response stream stalled") from exc
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield event


def _item_descriptor(item: dict) -> dict[str, str]:
    return {key: str(item[key]) for key in _ITEM_DESCRIPTOR_KEYS if item.get(key) is not None}


def _message_text(item: dict) -> str:
    return "".join(
        str(part.get("text", ""))
        for part in item.get("content") or []
        if part.get("type") in {"output_text", "text"}
    )


@dataclass
class _RequestTrace:
    """Identifiers for the HTTP call itself, reported back as provider metadata."""

    client_request_id: str
    request_id: str = ""
    rate_limits: dict[str, str] = field(default_factory=dict)

    def observe(self, headers: httpx.Headers) -> None:
        self.request_id = headers.get("x-request-id", "")
        self.rate_limits = {
            key: value for key, value in headers.items() if key.lower().startswith("x-ratelimit-")
        }


class _ResponsesStream:
    """Folds a Responses-API event stream into one finished reply.

    The same reply arrives twice over: as deltas while it is generated, and again
    whole in `response.completed`. Both are read, the deltas because they are what
    streams to the user and the final envelope because it is authoritative.
    """

    def __init__(self, token_callback: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._emit = token_callback
        self.text = ""
        self.reasoning = ""
        self._calls_by_id: dict[str, dict[str, str]] = {}
        self._tokens_in = 0
        self._tokens_out = 0
        self._cached_tokens = 0
        self._cache_write_tokens = 0
        self._model = ""
        self._response_id = ""
        self._conversation_id = ""
        self._previous_response_id = ""
        self._item_ids: list[str] = []
        self._output_items: list[dict[str, str]] = []
        self._event_types: list[str] = []
        self._last_sequence_number: int | None = None

    async def add(self, event: dict) -> None:
        event_type = event.get("type")
        self._note_arrival(event, event_type)
        handler = _RESPONSE_EVENT_HANDLERS.get(str(event_type))
        if handler is not None:
            await handler(self, event)

    def to_result(self, provider_name: str, trace: _RequestTrace) -> dict[str, Any]:
        calls = [self._tool_call(item) for item in self._calls_by_id.values()]
        if not self.text and not calls:
            raise InferenceError("OpenAI Codex returned no text or tool calls")
        return {
            "text": self.text,
            "reasoning": self.reasoning,
            "calls": calls,
            "tokens_in": self._tokens_in,
            "tokens_out": self._tokens_out,
            "cached_tokens": self._cached_tokens,
            "cache_write_tokens": self._cache_write_tokens,
            "model": self._model,
            "metadata": {
                "provider": provider_name,
                "response_id": self._response_id,
                "conversation_id": self._conversation_id,
                "previous_response_id": self._previous_response_id,
                "item_ids": self._item_ids,
                "output_items": self._output_items,
                "event_types": self._event_types,
                "last_sequence_number": self._last_sequence_number,
                "request_id": trace.request_id,
                "client_request_id": trace.client_request_id,
                "rate_limits": trace.rate_limits,
                "stored_remotely": False,
            },
        }

    def _note_arrival(self, event: dict, event_type: object) -> None:
        if isinstance(event.get("sequence_number"), int):
            self._last_sequence_number = event["sequence_number"]
        if isinstance(event_type, str) and event_type not in self._event_types:
            self._event_types.append(event_type)

    async def _on_response_opened(self, event: dict) -> None:
        response = event.get("response")
        envelope = response if isinstance(response, dict) else {}
        self._response_id = str(envelope.get("id") or self._response_id)
        self._remember_conversation(envelope)

    async def _on_text_delta(self, event: dict) -> None:
        delta = str(event.get("delta", ""))
        self.text += delta
        if self._emit and delta:
            await self._emit(delta)

    async def _on_reasoning_delta(self, event: dict) -> None:
        self.reasoning += str(event.get("delta", ""))

    async def _on_output_item(self, event: dict) -> None:
        item = event.get("item", {})
        self._remember_item(item)
        if item.get("type") != "function_call":
            return
        key = str(item.get("id") or item.get("call_id") or event.get("output_index", ""))
        entry = self._calls_by_id.setdefault(
            key, {"call_id": str(item.get("call_id") or key), "name": "", "arguments": ""}
        )
        if item.get("call_id"):
            entry["call_id"] = str(item["call_id"])
        if item.get("name"):
            entry["name"] = str(item["name"])
        if isinstance(item.get("arguments"), str):
            entry["arguments"] = item["arguments"]

    async def _on_arguments_delta(self, event: dict) -> None:
        key = str(event.get("item_id") or event.get("output_index", ""))
        entry = self._calls_by_id.setdefault(key, {"call_id": key, "name": "", "arguments": ""})
        entry["arguments"] += str(event.get("delta", ""))

    async def _on_completed(self, event: dict) -> None:
        completed = event.get("response", {})
        self._read_usage(completed.get("usage") or {})
        self._model = str(completed.get("model") or "")
        self._response_id = str(completed.get("id") or self._response_id)
        self._previous_response_id = str(completed.get("previous_response_id") or "")
        self._remember_conversation(completed)
        for item in completed.get("output") or []:
            self._remember_item(item)
            self._read_completed_item(item)

    async def _on_failed(self, event: dict) -> None:
        detail = event.get("error") or event.get("response", {}).get("error") or event
        raise InferenceError(f"OpenAI Codex response failed: {str(detail)[:_FAILURE_DETAIL_CHARS]}")

    def _read_usage(self, usage: dict) -> None:
        self._tokens_in = int(usage.get("input_tokens") or 0)
        self._tokens_out = int(usage.get("output_tokens") or 0)
        self._cached_tokens, self._cache_write_tokens = cache_tokens(usage)
        # `north inference costs` can say "0 tokens reused" for two very
        # different reasons: the cache genuinely missed, or this backend never
        # reports the number and the question is unanswerable. The keys the
        # provider actually sent settle it, and nothing else records them.
        logger.debug("codex usage keys=%s cached=%d", sorted(usage), self._cached_tokens)

    def _read_completed_item(self, item: dict) -> None:
        if item.get("type") == "message" and not self.text:
            self.text = _message_text(item)
        elif item.get("type") == "function_call":
            key = str(item.get("id") or item.get("call_id") or len(self._calls_by_id))
            self._calls_by_id[key] = {
                "call_id": str(item.get("call_id") or key),
                "name": str(item.get("name") or ""),
                "arguments": str(item.get("arguments") or "{}"),
            }

    def _remember_conversation(self, envelope: dict) -> None:
        conversation = envelope.get("conversation")
        if isinstance(conversation, dict):
            self._conversation_id = str(conversation.get("id") or self._conversation_id)

    def _remember_item(self, item: dict) -> None:
        item_id = str(item.get("id") or "")
        if item_id and item_id not in self._item_ids:
            self._item_ids.append(item_id)
        descriptor = _item_descriptor(item)
        if descriptor and descriptor not in self._output_items:
            self._output_items.append(descriptor)

    @staticmethod
    def _tool_call(item: dict[str, str]) -> ToolCall:
        try:
            params = json.loads(item["arguments"] or "{}")
        except json.JSONDecodeError:
            params = {"_raw": item["arguments"]}
        return ToolCall(name=item["name"], call_id=item["call_id"], params=params)


# Every Responses event north reads, mapped to the fold that reads it. Events
# with no entry are recorded as having arrived and otherwise ignored.
_RESPONSE_EVENT_HANDLERS: dict[str, Callable[[_ResponsesStream, dict], Awaitable[None]]] = {
    "response.created": _ResponsesStream._on_response_opened,
    "response.in_progress": _ResponsesStream._on_response_opened,
    "response.output_text.delta": _ResponsesStream._on_text_delta,
    "response.reasoning_text.delta": _ResponsesStream._on_reasoning_delta,
    "response.reasoning_summary_text.delta": _ResponsesStream._on_reasoning_delta,
    "response.output_item.added": _ResponsesStream._on_output_item,
    "response.output_item.done": _ResponsesStream._on_output_item,
    "response.function_call_arguments.delta": _ResponsesStream._on_arguments_delta,
    "response.completed": _ResponsesStream._on_completed,
    "response.failed": _ResponsesStream._on_failed,
    "error": _ResponsesStream._on_failed,
}

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _tool_definition(tool: dict) -> dict:
    function = tool.get("function", tool)
    result = {
        "type": "function",
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "parameters": function.get("parameters") or function.get("parameters_schema") or {"type": "object"},
    }
    if "strict" in function:
        result["strict"] = bool(function["strict"])
    return result


def _tool_output_item(message: dict) -> dict:
    content = message.get("content")
    return {
        "type": "function_call_output",
        "call_id": str(message.get("tool_call_id", "")),
        "output": content if isinstance(content, str) else json.dumps(content),
    }


def _assistant_call_items(message: dict) -> list[dict]:
    """The assistant's own words, if any, followed by each tool call it made."""
    items: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        items.append({"role": "assistant", "content": [{"type": "output_text", "text": content}]})
    for call in message["tool_calls"]:
        function = call.get("function", {})
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        items.append({
            "type": "function_call",
            "call_id": str(call.get("id", "")),
            "name": str(function.get("name", "")),
            "arguments": arguments,
        })
    return items


def _content_item(role: str, content: Any) -> dict:
    """One turn of plain content, whether it is text or text mixed with images."""
    text_type = "output_text" if role == "assistant" else "input_text"
    if not isinstance(content, list):
        return {"role": role, "content": [{"type": text_type, "text": str(content or "")}]}
    parts = [
        {"type": "input_image", "image_url": part.get("image_url", {}).get("url", "")}
        if part.get("type") == "image_url"
        else {"type": text_type, "text": str(part.get("text", ""))}
        for part in content
    ]
    return {"role": role, "content": parts}


def _message_items(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert North's Chat Completions history to Responses input items."""
    instructions: list[str] = []
    items: list[dict] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content")
        if role in {"system", "developer"}:
            if isinstance(content, str) and content:
                instructions.append(content)
        elif role == "tool":
            items.append(_tool_output_item(message))
        elif role == "assistant" and message.get("tool_calls"):
            items.extend(_assistant_call_items(message))
        else:
            items.append(_content_item(role, content))
    return "\n\n".join(instructions), items


class OpenAICodexProvider:
    """Codex subscription inference normalized to North's Provider contract."""

    name = "openai_codex"

    def __init__(
        self,
        credentials: CredentialProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._models: dict[str, ModelInfo] = {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=CODEX_BASE_URL,
            timeout=httpx.Timeout(timeout=DEFAULT_TIMEOUT_SECONDS, connect=5.0),
            headers={
                "accept": "text/event-stream",
                "content-type": "application/json",
                "OpenAI-Beta": "responses=experimental",
                "originator": "north",
                "user-agent": "north",
            },
        )

    async def _request_headers(self) -> dict[str, str]:
        """Return auth headers explicitly, including for injected test clients."""
        return (await self._credentials.get_auth()).headers

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        try:
            response = await self._client.get(
                "/models",
                params={"client_version": "1.0.0"},
                headers=await self._request_headers(),
            )
        except httpx.RequestError as exc:
            raise PoolRefreshError(f"OpenAI Codex /models request failed: {exc}") from exc
        self._raise_status(response, "models")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PoolRefreshError("OpenAI Codex /models response was not JSON") from exc
        rows = payload.get("data") or payload.get("models") or []
        live: dict[str, ModelInfo] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id") or row.get("slug") or row.get("model")
            if not isinstance(model_id, str) or not model_id:
                continue
            context_window = int(row.get("context_window") or row.get("context_window_tokens") or 200_000)
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name=self.name,
                capabilities=frozenset({
                    ModelCapability.COMPLETION,
                    ModelCapability.TOOL_CALLS,
                    ModelCapability.REASONING,
                }),
                context_window=context_window,
                cost_per_token=0.0,
                base_quality=0.9,
                max_payload_chars=min(context_window * 3, 400_000),
            )
        if not live:
            raise PoolRefreshError("OpenAI Codex returned no usable models")
        self._models = live

    def _raise_status(self, response: httpx.Response, model_id: str) -> None:
        if response.status_code == 401:
            raise ProviderAuthError("OpenAI Codex authentication failed; run `north auth login openai-codex`")
        if response.status_code == 404:
            raise ModelNotFoundError(model_id, self.name, status_code=404)
        if response.status_code == 429:
            retry = response.headers.get("retry-after")
            try:
                retry_after = float(retry) if retry else None
            except ValueError:
                retry_after = None
            raise ModelRateLimitedError(model_id, self.name, retry_after=retry_after, status_code=429)
        if response.status_code in {502, 503, 504}:
            raise ProviderUnavailableError(f"OpenAI Codex returned {response.status_code}")
        if response.status_code >= 400:
            raise InferenceError(f"OpenAI Codex returned {response.status_code}: {response.text[:200]}")

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        content: list[dict] = [{"type": "input_text", "text": request.prompt}]
        for encoded, mime_type in request.images:
            content.append({"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"})
        body: dict[str, Any] = {
            "model": model_id,
            "store": False,
            "stream": True,
            "input": [{"role": "user", "content": content}],
        }
        # Read through structured_schema, never response_schema directly: the raw
        # field may be a *bare* JSON Schema carrying its own "type": "object",
        # which spread into this dict silently overwrote "type": "json_schema".
        # Every such request was rejected with 400, which is a declared-capability
        # failure - so the router concluded these models could not do structured
        # output at all and routed every schema-enforced part away from the
        # subscription. Responses wants name/schema/strict flattened beside the
        # type, which is exactly the shape structured_schema returns.
        schema = request.structured_schema
        if schema is not None:
            body["text"] = {"format": {"type": "json_schema", **schema}}
        elif request.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        result = await self._stream(model_id, body, run_id=request.run_id)
        if result["calls"]:
            raise InferenceError("OpenAI Codex unexpectedly returned a tool call for a completion request")
        return CompletionResponse(
            text=result["text"],
            model_used=result["model"] or model_id,
            tokens_in=result["tokens_in"],
            cached_tokens=result["cached_tokens"],
            cache_write_tokens=result["cache_write_tokens"],
            tokens_out=result["tokens_out"],
            cost_usd=0.0,
            reasoning=result["reasoning"] or None,
            provider_metadata=result["metadata"],
        )

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        instructions, inputs = _message_items(request.messages)
        body: dict[str, Any] = {
            "model": model_id,
            "store": False,
            "stream": True,
            "input": inputs,
            "tools": [_tool_definition(tool) for tool in request.tools],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if instructions:
            body["instructions"] = instructions
        result = await self._stream(model_id, body, token_callback, run_id=request.run_id)
        calls = result["calls"]
        return ToolCallResponse(
            type="tool_calls" if calls else "message",
            content=result["text"] or None,
            calls=calls,
            model_used=result["model"] or model_id,
            tokens_in=result["tokens_in"],
            cached_tokens=result["cached_tokens"],
            cache_write_tokens=result["cache_write_tokens"],
            tokens_out=result["tokens_out"],
            cost_usd=0.0,
            reasoning=result["reasoning"] or None,
            provider_metadata=result["metadata"],
        )

    async def _stream(
        self,
        model_id: str,
        body: dict[str, Any],
        token_callback: Callable[[str], Awaitable[None]] | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        trace = _RequestTrace(client_request_id=f"{run_id or 'north'}:{generate_id()}")
        stream = _ResponsesStream(token_callback)
        try:
            headers = dict(await self._request_headers())
            headers["X-Client-Request-Id"] = trace.client_request_id
            async with self._client.stream("POST", "/responses", json=body, headers=headers) as response:
                trace.observe(response.headers)
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_status(response, model_id)
                async for event in _aiter_response_events(response):
                    await stream.add(event)
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(f"OpenAI Codex request failed: {exc}") from exc
        return stream.to_result(self.name, trace)

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        del model_id, request
        raise InferenceError("OpenAI Codex does not expose embeddings through this provider")

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        del model_id, request
        raise TranscriptionError("OpenAI Codex does not expose transcription through this provider")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
