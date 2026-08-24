"""Tests for out-of-band reasoning token streaming and context window resolution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import PaymentRequiredError
from inference.models import ToolCallRequest, ToolCallResponse
from inference.providers.openai_compat import OpenAICompatibleProvider


class _MockStreamTransport(httpx.AsyncBaseTransport):
    def __init__(self, sse_lines: list[str], status_code: int = 200) -> None:
        self._lines = sse_lines
        self._status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def body_stream() -> AsyncIterator[bytes]:
            for line in self._lines:
                yield f"{line}\n\n".encode()

        return httpx.Response(
            status_code=self._status_code,
            headers={"content-type": "text/event-stream"},
            content=body_stream(),
        )


@pytest.mark.asyncio
async def test_reasoning_tokens_streamed_and_fallback_to_content() -> None:
    """When a model only returns reasoning tokens and empty content, it is preserved."""
    sse_events = [
        "data: " + json.dumps({
            "choices": [{"delta": {"reasoning": "Let me think about this step by step..."}}],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {"reasoning": "\nHere is the answer: 42."}}],
        }),
        "data: [DONE]",
    ]
    client = httpx.AsyncClient(transport=_MockStreamTransport(sse_events), base_url="http://test")
    provider = OpenAICompatibleProvider(name="test", base_url="http://test", api_key="k")
    provider._client = client

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    req = ToolCallRequest(
        messages=[{"role": "user", "content": "What is the answer?"}],
        tools=[],
        component="test",
    )
    resp = await provider.complete_with_tools("test-reasoning-model", req, token_callback=on_token)

    assert isinstance(resp, ToolCallResponse)
    assert resp.type == "message"
    # Fallback captured reasoning text as answer
    assert "42" in resp.content
    assert resp.reasoning == "Let me think about this step by step...\nHere is the answer: 42."
    # Token callback received <thought> tags
    assert "<thought>" in tokens
    assert "</thought>" in tokens


@pytest.mark.asyncio
async def test_reasoning_with_subsequent_content() -> None:
    """When a model streams reasoning then final content, tokens and content are distinct."""
    sse_events = [
        "data: " + json.dumps({
            "choices": [{"delta": {"reasoning": "Thinking..."}}],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "Final answer."}}],
        }),
        "data: [DONE]",
    ]
    client = httpx.AsyncClient(transport=_MockStreamTransport(sse_events), base_url="http://test")
    provider = OpenAICompatibleProvider(name="test", base_url="http://test", api_key="k")
    provider._client = client

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    req = ToolCallRequest(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
        component="test",
    )
    resp = await provider.complete_with_tools("test-model", req, token_callback=on_token)

    assert resp.content == "Final answer."
    assert resp.reasoning == "Thinking..."
    assert "<thought>" in tokens
    assert "</thought>" in tokens
    assert "Final answer." in tokens


def test_403_raises_payment_required_isolated_to_model() -> None:
    """403 on a model raises PaymentRequiredError, preventing full provider lockout."""
    provider = OpenAICompatibleProvider(name="openrouter", base_url="http://test", api_key="k")
    with pytest.raises(PaymentRequiredError):
        provider._raise_cooldown_status(httpx.Response(403), "openrouter/some-restricted-model")


def test_dispatcher_get_context_window_from_registry() -> None:
    """Dispatcher returns live API context window for models in its registry."""
    class DummyProvider:
        name = "test_prov"

        def get_models(self) -> dict[str, ModelInfo]:
            return {
                "stealth/ox-alpha": ModelInfo(
                    model_id="stealth/ox-alpha",
                    provider_name="test_prov",
                    context_window=128000,
                    capabilities={ModelCapability.TOOL_CALLS, ModelCapability.COMPLETION},
                    cost_per_token=0.0,
                    base_quality=0.8,
                ),
                "google/gemini-2.5-pro": ModelInfo(
                    model_id="google/gemini-2.5-pro",
                    provider_name="test_prov",
                    context_window=1000000,
                    capabilities={ModelCapability.TOOL_CALLS, ModelCapability.COMPLETION},
                    cost_per_token=0.0,
                    base_quality=0.9,
                ),
            }

        async def refresh(self) -> None:
            pass

    dispatcher = ModelDispatcher(providers=[DummyProvider()])
    assert dispatcher.get_context_window("stealth/ox-alpha") == 128000
    assert dispatcher.get_context_window("google/gemini-2.5-pro") == 1000000
    assert dispatcher.get_context_window("unknown-model") == 128000
