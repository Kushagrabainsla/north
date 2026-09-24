"""Anthropic provider: the OpenAI<->Anthropic translation this codebase's agent
loop depends on, and mapping the SDK's typed errors onto this codebase's own.

Response-side tests use plain SimpleNamespace fakes rather than constructing
real anthropic.types.Message objects - `_tool_call_response`/`_usage_fields`
only ever duck-type `.type`/`.text`/`.name`/`.id`/`.input`/`.usage.*`, and the
real field names those fakes mirror were confirmed against the installed SDK
(anthropic.types.Message/Usage/TextBlock/ToolUseBlock) before this was written.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from inference.exceptions import (
    InferenceError,
    ModelDegenerateError,
    ModelNotFoundError,
    ModelRateLimitedError,
    ModelRefusedError,
    PaymentRequiredError,
    ProviderAuthError,
    ProviderUnavailableError,
)
from inference.providers.anthropic_provider import (
    _cost_usd,
    _model_price,
    _reraise_as_north_error,
    _to_anthropic_messages,
    _to_anthropic_tools,
    _tool_call_response,
    _usage_fields,
)

# ---------------------------------------------------------------------- #
# Message translation
# ---------------------------------------------------------------------- #


def test_leading_system_message_becomes_the_top_level_system_field():
    system, messages = _to_anthropic_messages(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "You are terse."
    assert messages == [{"role": "user", "content": "hi"}]


def test_mid_conversation_system_message_folds_into_a_user_aside():
    """Not every Claude model supports Anthropic's own mid-conversation system
    role, and this provider has no way to know in advance which one a given
    call will land on - so a system message past position 0 (the agent loop's
    soft-budget nudge is the real-world source of these) becomes a plain user
    aside instead, which every model accepts."""
    system, messages = _to_anthropic_messages(
        [
            {"role": "system", "content": "Top-level prompt."},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "working on it"},
            {"role": "system", "content": "Soft efficiency budget reached."},
        ]
    )
    assert system == "Top-level prompt."
    assert messages[-1]["role"] == "user"
    assert "[System note] Soft efficiency budget reached." in messages[-1]["content"][0]["text"]


def test_assistant_tool_calls_become_tool_use_blocks():
    _, messages = _to_anthropic_messages(
        [
            {"role": "user", "content": "what's the weather"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
                ],
            },
        ]
    )
    assistant = messages[-1]
    assert assistant["role"] == "assistant"
    tool_use = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert tool_use == {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}


def test_adjacent_tool_results_merge_into_one_user_message():
    """Anthropic rejects a tool_use turn whose results are split across more
    than one following message - two OpenAI `tool` messages in a row must
    become one Anthropic user message with two tool_result blocks."""
    _, messages = _to_anthropic_messages(
        [
            {"role": "user", "content": "do two things"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result a"},
            {"role": "tool", "tool_call_id": "call_2", "content": "result b"},
        ]
    )
    tool_result_message = messages[-1]
    assert tool_result_message["role"] == "user"
    assert len(tool_result_message["content"]) == 2
    assert tool_result_message["content"][0] == {"type": "tool_result", "tool_use_id": "call_1", "content": "result a"}
    assert tool_result_message["content"][1] == {"type": "tool_result", "tool_use_id": "call_2", "content": "result b"}


def test_vision_image_url_becomes_anthropic_base64_image_block():
    _, messages = _to_anthropic_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            }
        ]
    )
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}


def test_plain_string_user_content_passes_through_unchanged():
    _, messages = _to_anthropic_messages([{"role": "user", "content": "hello"}])
    assert messages == [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------- #
# Tool translation
# ---------------------------------------------------------------------- #


def test_openai_function_tool_becomes_anthropic_flat_shape():
    tools = _to_anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
    )
    assert tools == [
        {
            "name": "get_weather",
            "description": "Get the weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]


# ---------------------------------------------------------------------- #
# Pricing
# ---------------------------------------------------------------------- #


def test_known_model_reports_its_published_price():
    price, known = _model_price("claude-opus-5")
    assert known is True
    assert price > 0


def test_unknown_model_falls_back_without_claiming_a_published_price():
    price, known = _model_price("claude-some-future-model")
    assert known is False
    assert price > 0  # still a usable estimate for routing, just not presented as fact


def test_cost_uses_the_output_rate_for_both_directions():
    # Deliberately overstating cost (treating input tokens at the output rate)
    # rather than understating it - see the comment on _cost_usd.
    assert _cost_usd(1000, 1000, "claude-opus-5") == pytest.approx(2000 * 25.00e-6)


# ---------------------------------------------------------------------- #
# Response translation
# ---------------------------------------------------------------------- #


def _usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(call_id, name, input_):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=input_)


def test_text_only_message_becomes_a_message_type_response():
    message = SimpleNamespace(
        content=[_text_block("hello there")], usage=_usage(), stop_reason="end_turn", stop_details=None
    )
    response = _tool_call_response(message, "claude-opus-5", "anthropic")
    assert response.type == "message"
    assert response.content == "hello there"
    assert response.calls == []
    assert response.tokens_in == 10
    assert response.tokens_out == 5


def test_tool_use_blocks_become_a_tool_calls_response():
    message = SimpleNamespace(
        content=[_tool_use_block("call_1", "search", {"q": "north"})],
        usage=_usage(),
        stop_reason="tool_use",
        stop_details=None,
    )
    response = _tool_call_response(message, "claude-opus-5", "anthropic")
    assert response.type == "tool_calls"
    assert response.content is None
    assert response.calls[0].name == "search"
    assert response.calls[0].call_id == "call_1"
    assert response.calls[0].params == {"q": "north"}


def test_refusal_stop_reason_raises_model_refused_not_a_crash():
    message = SimpleNamespace(
        content=[_text_block("")],
        usage=_usage(),
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber"),
    )
    with pytest.raises(ModelRefusedError):
        _tool_call_response(message, "claude-opus-5", "anthropic")


def test_empty_content_raises_degenerate_not_a_silent_empty_answer():
    message = SimpleNamespace(content=[], usage=_usage(), stop_reason="end_turn", stop_details=None)
    with pytest.raises(ModelDegenerateError):
        _tool_call_response(message, "claude-opus-5", "anthropic")


def test_usage_fields_default_to_zero_when_missing():
    usage = _usage_fields(SimpleNamespace())
    assert usage.tokens_in == 0
    assert usage.cached_tokens == 0


# ---------------------------------------------------------------------- #
# Error mapping - matches OpenAICompatibleProvider._raise_cooldown_status's
# scoping for the same HTTP codes, so ChainWalk treats a Claude failure the
# same way it treats the same-shaped failure from any other provider.
# ---------------------------------------------------------------------- #


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status_code, headers=None):
    response = httpx2.Response(status_code, request=_request(), headers=headers or {})
    return cls(f"error {status_code}", response=response, body=None)


def test_authentication_error_maps_to_provider_auth_error():
    with pytest.raises(ProviderAuthError):
        _reraise_as_north_error(_status_error(anthropic.AuthenticationError, 401), "claude-opus-5", "anthropic")


def test_permission_denied_maps_to_payment_required():
    with pytest.raises(PaymentRequiredError):
        _reraise_as_north_error(_status_error(anthropic.PermissionDeniedError, 403), "claude-opus-5", "anthropic")


def test_not_found_maps_to_model_not_found():
    with pytest.raises(ModelNotFoundError):
        _reraise_as_north_error(_status_error(anthropic.NotFoundError, 404), "claude-opus-5", "anthropic")


def test_rate_limit_maps_to_model_rate_limited_with_retry_after():
    exc = _status_error(anthropic.RateLimitError, 429, headers={"retry-after": "12"})
    with pytest.raises(ModelRateLimitedError) as excinfo:
        _reraise_as_north_error(exc, "claude-opus-5", "anthropic")
    assert excinfo.value.retry_after == 12.0


def test_server_error_maps_to_provider_unavailable():
    with pytest.raises(ProviderUnavailableError):
        _reraise_as_north_error(_status_error(anthropic.APIStatusError, 503), "claude-opus-5", "anthropic")


def test_connection_error_maps_to_provider_unavailable():
    exc = anthropic.APIConnectionError(request=_request())
    with pytest.raises(ProviderUnavailableError):
        _reraise_as_north_error(exc, "claude-opus-5", "anthropic")


def test_unmapped_client_error_falls_through_to_inference_error():
    with pytest.raises(InferenceError):
        _reraise_as_north_error(_status_error(anthropic.APIStatusError, 400), "claude-opus-5", "anthropic")
