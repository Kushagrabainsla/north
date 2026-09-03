"""Experimental OpenAI Codex provider using the Responses wire protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
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
from utils.ids import generate_id

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
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id", "")),
                "output": content if isinstance(content, str) else json.dumps(content),
            })
            continue
        if role == "assistant" and message.get("tool_calls"):
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
            continue
        if isinstance(content, list):
            converted: list[dict] = []
            for part in content:
                if part.get("type") == "image_url":
                    image = part.get("image_url", {})
                    converted.append({"type": "input_image", "image_url": image.get("url", "")})
                else:
                    converted.append({
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": str(part.get("text", "")),
                    })
            items.append({"role": role, "content": converted})
        else:
            items.append({
                "role": role,
                "content": [{
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": str(content or ""),
                }],
            })
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
        text = ""
        reasoning = ""
        calls_by_id: dict[str, dict[str, str]] = {}
        tokens_in = 0
        tokens_out = 0
        response_model = ""
        response_id = ""
        conversation_id = ""
        previous_response_id = ""
        item_ids: list[str] = []
        output_items: list[dict[str, str]] = []
        event_types: list[str] = []
        seen_event_types: set[str] = set()
        last_sequence_number: int | None = None
        client_request_id = f"{run_id or 'north'}:{generate_id()}"
        request_id = ""
        rate_limits: dict[str, str] = {}
        try:
            headers = dict(await self._request_headers())
            headers["X-Client-Request-Id"] = client_request_id
            async with self._client.stream(
                "POST", "/responses", json=body, headers=headers
            ) as response:
                request_id = response.headers.get("x-request-id", "")
                rate_limits = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower().startswith("x-ratelimit-")
                }
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_status(response, model_id)
                iterator = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            iterator.__anext__(), timeout=SSE_CHUNK_TIMEOUT_SECONDS
                        )
                    except StopAsyncIteration:
                        break
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
                    event_type = event.get("type")
                    if isinstance(event.get("sequence_number"), int):
                        last_sequence_number = event["sequence_number"]
                    if isinstance(event_type, str) and event_type not in seen_event_types:
                        seen_event_types.add(event_type)
                        event_types.append(event_type)
                    envelope = event.get("response") if isinstance(event.get("response"), dict) else {}
                    if event_type in {"response.created", "response.in_progress"}:
                        response_id = str(envelope.get("id") or response_id)
                        conversation = envelope.get("conversation")
                        if isinstance(conversation, dict):
                            conversation_id = str(conversation.get("id") or conversation_id)
                    if event_type == "response.output_text.delta":
                        delta = str(event.get("delta", ""))
                        text += delta
                        if token_callback and delta:
                            await token_callback(delta)
                    elif event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
                        reasoning += str(event.get("delta", ""))
                    elif event_type in {"response.output_item.added", "response.output_item.done"}:
                        item = event.get("item", {})
                        item_id = str(item.get("id") or "")
                        if item_id and item_id not in item_ids:
                            item_ids.append(item_id)
                        descriptor = {
                            key: str(item[key])
                            for key in ("id", "type", "status", "name", "call_id")
                            if item.get(key) is not None
                        }
                        if descriptor and descriptor not in output_items:
                            output_items.append(descriptor)
                        if item.get("type") == "function_call":
                            key = str(item.get("id") or item.get("call_id") or event.get("output_index", ""))
                            entry = calls_by_id.setdefault(
                                key, {"call_id": str(item.get("call_id") or key), "name": "", "arguments": ""}
                            )
                            if item.get("call_id"):
                                entry["call_id"] = str(item["call_id"])
                            if item.get("name"):
                                entry["name"] = str(item["name"])
                            if isinstance(item.get("arguments"), str):
                                entry["arguments"] = item["arguments"]
                    elif event_type == "response.function_call_arguments.delta":
                        key = str(event.get("item_id") or event.get("output_index", ""))
                        entry = calls_by_id.setdefault(key, {"call_id": key, "name": "", "arguments": ""})
                        entry["arguments"] += str(event.get("delta", ""))
                    elif event_type == "response.completed":
                        completed = event.get("response", {})
                        usage = completed.get("usage") or {}
                        tokens_in = int(usage.get("input_tokens") or 0)
                        tokens_out = int(usage.get("output_tokens") or 0)
                        response_model = str(completed.get("model") or "")
                        response_id = str(completed.get("id") or response_id)
                        previous_response_id = str(completed.get("previous_response_id") or "")
                        conversation = completed.get("conversation")
                        if isinstance(conversation, dict):
                            conversation_id = str(conversation.get("id") or conversation_id)
                        for item in completed.get("output") or []:
                            item_id = str(item.get("id") or "")
                            if item_id and item_id not in item_ids:
                                item_ids.append(item_id)
                            descriptor = {
                                key: str(item[key])
                                for key in ("id", "type", "status", "name", "call_id")
                                if item.get(key) is not None
                            }
                            if descriptor and descriptor not in output_items:
                                output_items.append(descriptor)
                            if item.get("type") == "message" and not text:
                                text = "".join(
                                    str(part.get("text", ""))
                                    for part in item.get("content") or []
                                    if part.get("type") in {"output_text", "text"}
                                )
                            elif item.get("type") == "function_call":
                                key = str(item.get("id") or item.get("call_id") or len(calls_by_id))
                                calls_by_id[key] = {
                                    "call_id": str(item.get("call_id") or key),
                                    "name": str(item.get("name") or ""),
                                    "arguments": str(item.get("arguments") or "{}"),
                                }
                    elif event_type in {"response.failed", "error"}:
                        detail = event.get("error") or event.get("response", {}).get("error") or event
                        raise InferenceError(f"OpenAI Codex response failed: {str(detail)[:300]}")
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(f"OpenAI Codex request failed: {exc}") from exc
        calls: list[ToolCall] = []
        for item in calls_by_id.values():
            try:
                params = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError:
                params = {"_raw": item["arguments"]}
            calls.append(ToolCall(name=item["name"], call_id=item["call_id"], params=params))
        if not text and not calls:
            raise InferenceError("OpenAI Codex returned no text or tool calls")
        return {
            "text": text,
            "reasoning": reasoning,
            "calls": calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": response_model,
            "metadata": {
                "provider": self.name,
                "response_id": response_id,
                "conversation_id": conversation_id,
                "previous_response_id": previous_response_id,
                "item_ids": item_ids,
                "output_items": output_items,
                "event_types": event_types,
                "last_sequence_number": last_sequence_number,
                "request_id": request_id,
                "client_request_id": client_request_id,
                "rate_limits": rate_limits,
                "stored_remotely": False,
            },
        }

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        del model_id, request
        raise InferenceError("OpenAI Codex does not expose embeddings through this provider")

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        del model_id, request
        raise TranscriptionError("OpenAI Codex does not expose transcription through this provider")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
