"""Native Anthropic provider, on the official `anthropic` SDK.

Not an OpenAI-compatible shim. Anthropic's wire format differs in three ways
that a shim papers over badly: the system prompt is a top-level field rather
than a message, tool results are content blocks inside a user turn rather than
a `tool` role, and tools declare `input_schema` rather than `parameters`. The
conversions live in this file so the rest of north keeps speaking one format.

Auth is a Console API key (`sk-ant-api...`). A Claude Pro or Max subscription
does not authorise the developer API - the two are separate accounts - so this
provider stays inactive until NORTH_ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from inference.capability import ModelCapability, ModelInfo, quality_from_cost
from inference.constants import DEFAULT_TIMEOUT_SECONDS
from inference.exceptions import (
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    PayloadTooLargeError,
    PaymentRequiredError,
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

logger = logging.getLogger(__name__)

# Anthropic requires max_tokens on every request; the SDK has no default. These
# match the API guidance: room to answer without inviting an HTTP timeout on the
# non-streaming path, and a larger ceiling where streaming removes that risk.
_MAX_TOKENS_DEFAULT = 16_000
_MAX_TOKENS_STREAMING = 64_000

# Output price per token, USD. The Models API publishes capabilities and window
# sizes but not prices, and north ranks candidates by price - so the published
# figures live here, and anything absent is priced as a guess (price_known=False)
# rather than presented as a fact.
_OUTPUT_PRICE_PER_TOKEN: dict[str, float] = {
    "claude-fable-5-1": 50.0 / 1_000_000,
    "claude-fable-5": 50.0 / 1_000_000,
    "claude-opus-5": 25.0 / 1_000_000,
    "claude-opus-4-8": 25.0 / 1_000_000,
    "claude-opus-4-7": 25.0 / 1_000_000,
    "claude-opus-4-6": 25.0 / 1_000_000,
    "claude-sonnet-5": 10.0 / 1_000_000,
    "claude-sonnet-4-6": 15.0 / 1_000_000,
    "claude-haiku-4-5": 5.0 / 1_000_000,
}
_UNKNOWN_PRICE_PER_TOKEN = 25.0 / 1_000_000  # priced as Opus until the catalog says otherwise
_DEFAULT_CONTEXT_WINDOW = 200_000


def _tool_definitions(tools: list[dict]) -> list[dict]:
    """north's OpenAI-shaped tool list in the shape Anthropic expects."""
    converted: list[dict] = []
    for tool in tools:
        function = tool.get("function") if "function" in tool else tool
        name = function.get("name")
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": function.get("description", ""),
                "input_schema": function.get("parameters")
                or function.get("input_schema")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def _assistant_content(message: dict) -> list[dict] | str:
    """An assistant turn, carrying any tool calls it made as tool_use blocks."""
    calls = message.get("tool_calls") or []
    if not calls:
        return message.get("content") or ""

    blocks: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append({"type": "text", "text": text})
    for call in calls:
        function = call.get("function", {})
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            import json

            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id", "")),
                "name": str(function.get("name", "")),
                "input": arguments if isinstance(arguments, dict) else {},
            }
        )
    return blocks


def split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split north's history into (system prompt, Anthropic messages).

    Anthropic takes the system prompt as its own field, and answers a tool call
    with a `tool_result` block inside the *user* turn - there is no `tool` role.
    Consecutive tool results are merged into one user turn, which is also what
    the API wants when several tools ran in parallel.
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content")

        if role in {"system", "developer"}:
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id", "")),
                "content": content if isinstance(content, str) else str(content),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            converted.append({"role": "assistant", "content": _assistant_content(message)})
            continue

        converted.append({"role": "user", "content": content if content else ""})

    return "\n\n".join(system_parts), converted


def _text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def _tool_calls_of(message: Any) -> list[ToolCall]:
    return [
        ToolCall(name=block.name, call_id=block.id, params=dict(block.input or {}))
        for block in message.content
        if getattr(block, "type", "") == "tool_use"
    ]


class AnthropicProvider:
    """Claude models through Anthropic's own API."""

    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS)
        self._models: dict[str, ModelInfo] = {}

    # ---- catalogue ----

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Replace the model list from the Models API."""
        try:
            listing = await self._client.models.list()
        except Exception as exc:
            raise PoolRefreshError(f"Anthropic /models request failed: {exc}") from exc

        live: dict[str, ModelInfo] = {}
        for model in listing.data:
            model_id = getattr(model, "id", "")
            if not model_id:
                continue
            price = _OUTPUT_PRICE_PER_TOKEN.get(model_id, _UNKNOWN_PRICE_PER_TOKEN)
            live[model_id] = ModelInfo(
                model_id=model_id,
                provider_name=self.name,
                # Every current Claude model does completion, tools and vision;
                # the Models API reports capabilities but not under names north's
                # inference-from-id helper would recognise.
                capabilities=frozenset(
                    {
                        ModelCapability.COMPLETION,
                        ModelCapability.TOOL_CALLS,
                        ModelCapability.VISION,
                        ModelCapability.REASONING,
                    }
                ),
                context_window=int(getattr(model, "max_input_tokens", 0) or _DEFAULT_CONTEXT_WINDOW),
                cost_per_token=price,
                base_quality=quality_from_cost(price),
                price_known=model_id in _OUTPUT_PRICE_PER_TOKEN,
            )
        self._models = live

    # ---- completion ----

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        system, messages = split_messages([{"role": "user", "content": request.prompt}])
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": request.max_tokens or _MAX_TOKENS_DEFAULT,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        output_format = self._output_format(request)
        if output_format:
            kwargs["output_config"] = {"format": output_format}

        message = await self._send(kwargs, model_id)
        text = _text_of(message)
        if not text:
            raise ModelDegenerateError(model_id, self.name, reason="empty completion text")
        return CompletionResponse(
            text=text,
            model_used=getattr(message, "model", model_id),
            tokens_in=message.usage.input_tokens,
            tokens_out=message.usage.output_tokens,
            cost_usd=0.0,  # Anthropic does not price a response inline; the ledger costs it.
            cached_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        )

    @staticmethod
    def _output_format(request: CompletionRequest) -> dict | None:
        """north's JSON request expressed as Anthropic's output_config format."""
        schema = request.structured_schema
        if schema:
            return {"type": "json_schema", "schema": schema.get("schema", schema)}
        if request.json_mode:
            return {"type": "json_object"}
        return None

    # ---- tool calls ----

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        system, messages = split_messages(request.messages)
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": _MAX_TOKENS_STREAMING,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        tools = _tool_definitions(request.tools)
        if tools:
            kwargs["tools"] = tools

        message = await self._stream(kwargs, model_id, token_callback)
        calls = _tool_calls_of(message)
        text = _text_of(message)
        if not calls and not text:
            raise ModelDegenerateError(model_id, self.name, reason="empty stream (no content or tool calls)")
        return ToolCallResponse(
            type="tool_calls" if calls else "message",
            calls=calls,
            content=None if calls else text,
            model_used=getattr(message, "model", model_id),
            tokens_in=message.usage.input_tokens,
            tokens_out=message.usage.output_tokens,
            cost_usd=0.0,
            cached_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        )

    # ---- transport ----

    async def _send(self, kwargs: dict[str, Any], model_id: str) -> Any:
        try:
            return await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise self._translate(exc, model_id) from exc

    async def _stream(
        self,
        kwargs: dict[str, Any],
        model_id: str,
        token_callback: Callable[[str], Awaitable[None]] | None,
    ) -> Any:
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                if token_callback is not None:
                    async for text in stream.text_stream:
                        if text:
                            await token_callback(text)
                return await stream.get_final_message()
        except Exception as exc:
            raise self._translate(exc, model_id) from exc

    def _translate(self, exc: Exception, model_id: str) -> Exception:
        """Anthropic's SDK errors as the failures north's dispatcher routes on."""
        import anthropic

        if isinstance(exc, anthropic.RateLimitError):
            retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
            return ModelRateLimitedError(
                model_id,
                self.name,
                retry_after=float(retry_after) if retry_after else None,
                status_code=429,
            )
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return ProviderAuthError(f"Anthropic rejected the credentials: {exc}")
        if isinstance(exc, anthropic.NotFoundError):
            return ModelNotFoundError(model_id, self.name, status_code=404)
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code == 402:
                return PaymentRequiredError(model_id, self.name)
            if exc.status_code == 413:
                return PayloadTooLargeError(model_id, self.name)
            if exc.status_code >= 500:
                return ProviderUnavailableError(f"Anthropic returned {exc.status_code}: {exc}")
            return InferenceError(f"Anthropic request failed ({exc.status_code}): {exc}")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderUnavailableError(f"Connection to Anthropic failed: {exc}")
        return InferenceError(f"Anthropic request failed: {exc}")

    # ---- capabilities Anthropic does not serve ----

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        del model_id, request
        raise InferenceError("Anthropic does not expose embeddings")

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        del model_id, request
        raise TranscriptionError("Anthropic does not expose transcription")

    async def aclose(self) -> None:
        await self._client.close()
