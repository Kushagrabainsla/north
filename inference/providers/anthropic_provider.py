"""Anthropic (Claude) inference provider.

Not OpenAI-compatible - Claude's Messages API has its own shape (a top-level
`system` field, typed content blocks, no `choices[0].message`), so this does
not subclass `OpenAICompatibleProvider` the way Gemini/Groq/OpenCode Zen do.
It talks to Claude through the official `anthropic` SDK rather than hand-rolled
HTTP, per the project's own guidance for building on the Claude API.

`ToolCallRequest.messages`/`.tools` arrive in OpenAI's wire format - that is
what every agent in this codebase builds and what `context_compaction.py`
mutates in place - so this module's job is translating between that shape and
Anthropic's on every call, in both directions. See `_to_anthropic_messages`
and `_to_anthropic_tools` for the request side, `_tool_call_response` for the
response side.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic

from inference.capability import ModelCapability, ModelInfo, quality_from_cost
from inference.exceptions import (
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    ModelRefusedError,
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

# Non-streaming complete() calls are typically short (classification, judgement
# checks); streaming complete_with_tools() calls are the agent loop and can run
# long. Different defaults for the same reason north's own SDK guidance gives:
# a low non-streaming default stays well clear of the SDK's ~10-minute
# non-streaming timeout guard, a high streaming one doesn't cut off real work.
_DEFAULT_MAX_TOKENS_COMPLETE = 16_000
_DEFAULT_MAX_TOKENS_TOOLS = 64_000

# Anthropic does not publish price via the Models API, so context window comes
# from client.models.list() live, but price is this table - it is the one
# thing about a Claude model this provider cannot discover at runtime. Prices
# are USD per output token; ModelInfo.price_known=False for anything not
# listed, so an unrecognised model still routes (ranked by quality prior) but
# never reports a cost it does not actually know.
# Source: shared/live-sources.md pricing page (Claude API docs), checked 2026.
_KNOWN_MODEL_PRICE_PER_OUTPUT_TOKEN: dict[str, float] = {
    "claude-fable-5-1": 50.00e-6,
    "claude-fable-5": 50.00e-6,
    "claude-opus-5": 25.00e-6,
    "claude-opus-4-8": 25.00e-6,
    "claude-opus-4-7": 25.00e-6,
    "claude-opus-4-6": 25.00e-6,
    "claude-sonnet-5": 10.00e-6,
    "claude-sonnet-4-6": 15.00e-6,
    "claude-haiku-4-5": 5.00e-6,
}
# A generic fallback for a model this table has not been updated for yet -
# Sonnet-tier pricing, a reasonable middle estimate. price_known=False on any
# model that lands here, so routing never presents this as a published figure.
_FALLBACK_PRICE_PER_OUTPUT_TOKEN = 10.00e-6


def _model_price(model_id: str) -> tuple[float, bool]:
    """(price_per_output_token, price_known) for a live model id."""
    price = _KNOWN_MODEL_PRICE_PER_OUTPUT_TOKEN.get(model_id)
    if price is not None:
        return price, True
    return _FALLBACK_PRICE_PER_OUTPUT_TOKEN, False


# ---------------------------------------------------------------------- #
# Request translation: OpenAI wire format (what every caller in this
# codebase builds) -> Anthropic Messages API shape.
# ---------------------------------------------------------------------- #


def _text_of(content: Any) -> str:
    """Flatten OpenAI-shaped message content (string or a list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _to_anthropic_user_content(content: Any) -> str | list[dict]:
    """A user turn's content, translating OpenAI vision blocks to Anthropic's image shape.

    A plain string passes through unchanged - Anthropic accepts bare string
    content for a text-only turn, same as OpenAI does.
    """
    if isinstance(content, str) or content is None:
        return content or ""
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            media_type, _, data = url.partition(",")
            # "data:<mime>;base64,<data>" - the part between "data:" and ";base64" is the mime type.
            mime = media_type.removeprefix("data:").removesuffix(";base64")
            if data:
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": mime or "image/png", "data": data}}
                )
    return blocks


def _to_anthropic_assistant_content(message: dict) -> list[dict]:
    """An assistant turn's content: prior text plus any tool calls it made."""
    blocks: list[dict] = []
    content = message.get("content")
    text = _text_of(content) if content else ""
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "input": arguments,
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def _is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate one OpenAI-format transcript into (system_text, anthropic_messages).

    The leading system message becomes the top-level `system` field, matching
    every other message in this transcript's construction. Anything after it
    is a mid-conversation operator note (the agent loop's soft-budget nudge is
    the one produced today) - folded into a user aside rather than sent as
    Anthropic's own mid-conversation system role, since that role is not
    supported on every current Claude model and this provider has no way to
    know in advance which model will serve the call.

    Adjacent OpenAI `tool` messages must merge into a single Anthropic user
    message: the API rejects a `tool_use` turn whose `tool_result`s are split
    across more than one following message.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if not out:
                system_parts.append(_text_of(message.get("content")))
            else:
                note = _text_of(message.get("content"))
                out.append({"role": "user", "content": [{"type": "text", "text": f"[System note] {note}"}]})
            continue
        if role == "user":
            out.append({"role": "user", "content": _to_anthropic_user_content(message.get("content"))})
            continue
        if role == "assistant":
            out.append({"role": "assistant", "content": _to_anthropic_assistant_content(message)})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": _text_of(message.get("content")),
            }
            if out and _is_tool_result_message(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        logger.debug("Anthropic provider: dropping message with unrecognised role %r", role)
    return "\n\n".join(p for p in system_parts if p), out


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI function-calling defs -> Anthropic's flat name/description/input_schema shape."""
    out = []
    for tool in tools:
        function = tool.get("function", tool)
        out.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


# ---------------------------------------------------------------------- #
# Response translation: Anthropic Message -> this codebase's response shapes.
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Usage:
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cache_write_tokens: int


def _usage_fields(usage: Any) -> _Usage:
    return _Usage(
        tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
        cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def _cost_usd(tokens_in: int, tokens_out: int, model_id: str) -> float:
    price_per_output_token, _ = _model_price(model_id)
    # Anthropic does not publish a separate input price in a form this table
    # tracks per-model; using the output rate for input tokens overstates cost
    # (input is always cheaper than output) rather than understating it, which
    # is the safer direction for a number that feeds budget/spend tracking.
    return (tokens_in + tokens_out) * price_per_output_token


def _raise_if_degenerate(message: Any, model_id: str, provider_name: str) -> None:
    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        category = getattr(detail, "category", None) if detail is not None else None
        raise ModelRefusedError(
            model_id,
            provider_name,
            status_code=200,
            body={"stop_reason": "refusal", "category": category},
        )
    if not message.content:
        raise ModelDegenerateError(model_id, provider_name, reason="empty response (no content blocks)")


def _tool_call_response(message: Any, model_id: str, provider_name: str) -> ToolCallResponse:
    _raise_if_degenerate(message, model_id, provider_name)
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    for block in message.content:
        if block.type == "tool_use":
            calls.append(ToolCall(name=block.name, call_id=block.id, params=dict(block.input or {})))
        elif block.type == "text":
            text_parts.append(block.text)
    usage = _usage_fields(message.usage)
    cost = _cost_usd(usage.tokens_in, usage.tokens_out, model_id)
    if calls:
        return ToolCallResponse(
            type="tool_calls",
            calls=calls,
            content=None,
            model_used=model_id,
            cost_usd=cost,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
    content = "".join(text_parts)
    if not content:
        raise ModelDegenerateError(model_id, provider_name, reason="empty completion text and no tool calls")
    return ToolCallResponse(
        type="message",
        calls=[],
        content=content,
        model_used=model_id,
        cost_usd=cost,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


# ---------------------------------------------------------------------- #
# Error translation: the anthropic SDK's typed exceptions -> this codebase's
# own typed exceptions, which ModelDispatcher/ChainWalk classify by scope
# (inference/failure.py). Chosen to match what OpenAICompatibleProvider's
# _raise_cooldown_status does for the same HTTP codes, so a Claude failure is
# scoped the same way a same-shaped failure from any other provider would be.
# ---------------------------------------------------------------------- #


def _reraise_as_north_error(exc: Exception, model_id: str, provider_name: str) -> None:
    """Translate an anthropic SDK exception and raise its north equivalent. Always raises."""
    if isinstance(exc, anthropic.AuthenticationError):
        raise ProviderAuthError(f"{provider_name} returned 401 - provider auth failed") from exc
    if isinstance(exc, anthropic.PermissionDeniedError):
        # Anthropic's content/policy declines surface as stop_reason "refusal"
        # on a 200, not as an HTTP 403 - so a 403 here is an entitlement
        # problem (org/key lacks access to this model, account suspended),
        # the same shape OpenAICompatibleProvider treats as billing.
        raise PaymentRequiredError(model_id, provider_name, status_code=403) from exc
    if isinstance(exc, anthropic.NotFoundError):
        raise ModelNotFoundError(model_id, provider_name, status_code=404) from exc
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        header = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
        if header is not None:
            try:
                retry_after = float(header)
            except (TypeError, ValueError):
                retry_after = None
        raise ModelRateLimitedError(model_id, provider_name, retry_after=retry_after, status_code=429) from exc
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            raise ProviderUnavailableError(
                f"{provider_name} returned {exc.status_code} - gateway/server outage"
            ) from exc
        raise InferenceError(f"{provider_name} returned {exc.status_code} for {model_id}: {str(exc)[:200]}") from exc
    if isinstance(exc, anthropic.APIConnectionError):
        raise ProviderUnavailableError(f"Connection to {provider_name} failed: {exc}") from exc
    raise InferenceError(f"{provider_name} request failed for {model_id}: {exc}") from exc


class AnthropicProvider:
    """Claude provider: completions and tool calls via the official Anthropic SDK.

    No embeddings, no transcription - Claude does not serve either, so those
    two Provider methods exist only to raise a clear "not supported" error
    rather than silently returning nothing.
    """

    def __init__(self, api_key: str) -> None:
        self.name = "anthropic"
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._models: dict[str, ModelInfo] = {}

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def refresh(self) -> None:
        """Fetch the live, account-accessible model list from the Models API.

        Context window comes from here (`max_input_tokens`); price does not -
        see `_KNOWN_MODEL_PRICE_PER_OUTPUT_TOKEN` - the Models API does not
        publish it.
        """
        live: dict[str, ModelInfo] = {}
        try:
            async for model in self._client.models.list():
                price, price_known = _model_price(model.id)
                context_window = int(getattr(model, "max_input_tokens", 0) or 200_000)
                live[model.id] = ModelInfo(
                    model_id=model.id,
                    provider_name=self.name,
                    capabilities=frozenset(
                        {ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS, ModelCapability.VISION}
                    ),
                    context_window=context_window,
                    cost_per_token=price,
                    base_quality=quality_from_cost(price),
                    price_known=price_known,
                )
        except anthropic.APIConnectionError as e:
            raise PoolRefreshError(f"Anthropic models.list() request failed: {e}") from e
        except anthropic.APIStatusError as e:
            raise PoolRefreshError(f"Anthropic models.list() returned {e.status_code}") from e

        if live:
            self._models = live

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        # Sampling params (temperature/top_p/top_k) are not passed: current
        # Claude models (Opus 5, Sonnet 5, Fable 5/5.1) reject them outright,
        # and the installed SDK no longer even types `temperature` as a
        # messages.create() parameter - confirmed against the installed
        # package rather than assumed. request.temperature is silently
        # ignored for this provider only.
        kwargs: dict[str, Any] = {}
        if request.wants_json and request.response_schema is None:
            kwargs["system"] = "Respond with valid JSON only - no prose before or after it."
        schema = request.structured_schema
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema["schema"]}}
        try:
            message = await self._client.messages.create(
                model=model_id,
                max_tokens=request.max_tokens or _DEFAULT_MAX_TOKENS_COMPLETE,
                messages=[{"role": "user", "content": request.prompt}],
                **kwargs,
            )
        except anthropic.APIError as e:
            _reraise_as_north_error(e, model_id, self.name)
            raise  # unreachable - _reraise_as_north_error always raises
        _raise_if_degenerate(message, model_id, self.name)
        text = "".join(b.text for b in message.content if b.type == "text")
        if not text:
            raise ModelDegenerateError(model_id, self.name, reason="empty completion text")
        usage = _usage_fields(message.usage)
        return CompletionResponse(
            text=text,
            model_used=model_id,
            cost_usd=_cost_usd(usage.tokens_in, usage.tokens_out, model_id),
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    async def complete_with_tools(
        self,
        model_id: str,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        system, messages = _to_anthropic_messages(request.messages)
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system"] = system
        if request.tools:
            kwargs["tools"] = _to_anthropic_tools(request.tools)
        try:
            # messages/tools are plain dicts built by the translators above,
            # matching how ToolCallRequest.messages/.tools arrive (see the
            # module docstring) - the same "wire format as dict, not as the
            # SDK's TypedDicts" choice this codebase makes for every provider.
            async with self._client.messages.stream(
                model=model_id,
                max_tokens=_DEFAULT_MAX_TOKENS_TOOLS,
                messages=messages,  # type: ignore[arg-type]
                **kwargs,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                        and token_callback is not None
                    ):
                        await token_callback(event.delta.text)
                message = await stream.get_final_message()
        except anthropic.APIError as e:
            _reraise_as_north_error(e, model_id, self.name)
            raise  # unreachable
        return _tool_call_response(message, model_id, self.name)

    async def embed(self, model_id: str, request: EmbedRequest) -> EmbedResponse:
        raise InferenceError(f"{self.name} does not support embeddings")

    async def transcribe(self, model_id: str, request: TranscriptionRequest) -> TranscriptionResponse:
        raise TranscriptionError(f"{self.name} does not support transcription")
