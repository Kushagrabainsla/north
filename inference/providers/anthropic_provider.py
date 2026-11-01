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
from inference.transcript import message_text, system_notes_in_place

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
# thing about a Claude model this provider cannot discover at runtime.
#
# Each token class has its own rate. The table had one rate per model, the
# output rate, and charged input with it, and it predated the 5.5 models,
# which fell to a $10/M guess: Haiku 5.5 input was charged 100 times over, and
# north recorded $192.51 for calls that cost $12.86. A model missing from the
# table is unpriced, not guessed at: its calls record no cost.
# Source: Anthropic's pricing (claude-api skill reference), checked 2026-11-01.


@dataclass(frozen=True)
class _Rates:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        # A 5-minute cache entry, the only kind north writes, costs 1.25x input.
        return self.input * 1.25


_PRICES: dict[str, _Rates] = {
    "claude-fable-5-1": _Rates(10.00, 50.00, 0.25),
    "claude-mythos-5-1": _Rates(10.00, 50.00, 0.25),
    "claude-fable-5": _Rates(10.00, 50.00, 1.00),
    "claude-mythos-5": _Rates(10.00, 50.00, 1.00),
    "claude-opus-5-5": _Rates(4.00, 20.00, 0.20),
    "claude-opus-5": _Rates(5.00, 25.00, 0.50),
    "claude-opus-4-8": _Rates(5.00, 25.00, 0.50),
    "claude-opus-4-7": _Rates(5.00, 25.00, 0.50),
    "claude-opus-4-6": _Rates(5.00, 25.00, 0.50),
    "claude-sonnet-5-5": _Rates(2.00, 10.00, 0.20),
    "claude-sonnet-5": _Rates(2.00, 10.00, 0.20),
    "claude-sonnet-4-6": _Rates(3.00, 15.00, 0.30),
    "claude-haiku-5-5": _Rates(0.10, 0.50, 0.01),
    "claude-haiku-4-5": _Rates(1.00, 5.00, 0.10),
    # What models.list() returns for Haiku 4.5.
    "claude-haiku-4-5-20251001": _Rates(1.00, 5.00, 0.10),
}
# Above this many prompt tokens a call is billed at the long-prompt rates.
_LONG_PROMPT_PRICES: dict[str, tuple[int, _Rates]] = {
    "claude-haiku-5-5": (100_000, _Rates(0.50, 2.50, 0.05)),
}
# Routing still needs a figure to rank a model it cannot price; price_known=False
# says it is an estimate, and no call is ever charged at it.
_ROUTING_ESTIMATE_PER_OUTPUT_TOKEN = 10.00e-6


def _model_price(model_id: str) -> tuple[float, bool]:
    """(USD per output token, price_known) for ranking a live model."""
    rates = _PRICES.get(model_id)
    if rates is None:
        return _ROUTING_ESTIMATE_PER_OUTPUT_TOKEN, False
    return rates.output / 1_000_000, True


# ---------------------------------------------------------------------- #
# Request translation: OpenAI wire format (what every caller in this
# codebase builds) -> Anthropic Messages API shape.
# ---------------------------------------------------------------------- #


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
    text = message_text(content) if content else ""
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

    The leading system message becomes the top-level `system` field. A later
    one (the agent loop's soft-budget nudge) arrives as a user note in its place
    (see `system_notes_in_place`): Anthropic's own mid-conversation system role
    is not supported on every current Claude model, and merging it into `system`
    would change the cached start of the prompt.

    Adjacent OpenAI `tool` messages must merge into a single Anthropic user
    message: the API rejects a `tool_use` turn whose `tool_result`s are split
    across more than one following message.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for message in system_notes_in_place(messages):
        role = message.get("role")
        if role == "system":
            system_parts.append(message_text(message.get("content")))
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
                "content": message_text(message.get("content")),
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
    # The whole prompt, as every other provider reports it. Anthropic's
    # input_tokens is only the part read neither from nor into the cache, so
    # with caching on it would read as a few hundred tokens of a 30k prompt and
    # the agent loop, which compacts by prompt size, would never compact.
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cache_write_tokens: int

    @property
    def uncached_tokens(self) -> int:
        return self.tokens_in - self.cached_tokens - self.cache_write_tokens


def _usage_fields(usage: Any) -> _Usage:
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return _Usage(
        tokens_in=int(getattr(usage, "input_tokens", 0) or 0) + cached + written,
        tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
        cached_tokens=cached,
        cache_write_tokens=written,
    )


def _cost_usd(usage: _Usage, model_id: str) -> float | None:
    """What one call cost in USD, each token class at its own rate; None when the model is unpriced."""
    long_prompt = _LONG_PROMPT_PRICES.get(model_id)
    if long_prompt is not None and usage.tokens_in > long_prompt[0]:
        rates: _Rates | None = long_prompt[1]
    else:
        rates = _PRICES.get(model_id)
    if rates is None:
        return None
    per_million = (
        usage.uncached_tokens * rates.input
        + usage.cached_tokens * rates.cache_read
        + usage.cache_write_tokens * rates.cache_write
        + usage.tokens_out * rates.output
    )
    return per_million / 1_000_000


def _cost_fields(usage: _Usage, model_id: str) -> dict[str, Any]:
    """The usage and cost fields every response from this provider carries."""
    cost = _cost_usd(usage, model_id)
    return {
        "cost_usd": cost or 0.0,
        "priced": cost is not None,
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cached_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


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
    usage = _cost_fields(_usage_fields(message.usage), model_id)
    if calls:
        return ToolCallResponse(type="tool_calls", calls=calls, content=None, model_used=model_id, **usage)
    content = "".join(text_parts)
    if not content:
        raise ModelDegenerateError(model_id, provider_name, reason="empty completion text and no tool calls")
    return ToolCallResponse(type="message", calls=[], content=content, model_used=model_id, **usage)


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
        see `_PRICES` - the Models API does not publish it.
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
        return CompletionResponse(
            text=text, model_used=model_id, **_cost_fields(_usage_fields(message.usage), model_id)
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
