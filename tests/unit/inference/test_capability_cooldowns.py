"""Unit tests for capability-scoped circuit breakers and wire error detection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.exceptions import ModelDegenerateError
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    PoolPriority,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
)
from inference.provider import Provider
from inference.providers.openai_compat import OpenAICompatibleProvider


class FakeProvider(Provider):
    def __init__(self, name: str, models: dict[str, ModelInfo]) -> None:
        self._name = name
        self._models = models

    @property
    def name(self) -> str:
        return self._name

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text=f"Response from {model_id}",
            model_used=model_id,
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
        )

    async def complete_with_tools(
        self, model_id: str, request: ToolCallRequest, token_callback=None
    ) -> ToolCallResponse:
        if model_id == "gpt-5-flaky":
            raise ModelDegenerateError(model_id, self.name, reason="upstream stream error (network_error)")
        return ToolCallResponse(
            type="tool_calls",
            calls=[ToolCall(name="test_tool", call_id="call_1", params={})],
            content=None,
            model_used=model_id,
        )


@pytest.mark.asyncio
async def test_tool_failure_suspends_only_tool_capability() -> None:
    model1 = ModelInfo(
        model_id="gpt-5-flaky",
        provider_name="fake",
        capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        context_window=128000,
        cost_per_token=0.0,
        base_quality=0.95,
    )
    model2 = ModelInfo(
        model_id="gpt-4o-mini-working",
        provider_name="fake",
        capabilities=frozenset({ModelCapability.COMPLETION, ModelCapability.TOOL_CALLS}),
        context_window=128000,
        cost_per_token=0.0,
        base_quality=0.50,
    )
    provider = FakeProvider("fake", {"gpt-5-flaky": model1, "gpt-4o-mini-working": model2})
    dispatcher = ModelDispatcher([provider])

    # 1. Dispatch complete_with_tools: flaky model fails, dispatcher falls over to model2
    req = ToolCallRequest(
        messages=[{"role": "user", "content": "run tool"}],
        tools=[{"name": "test_tool"}],
        priority=PoolPriority.HIGH,
        component="test",
    )
    resp = await dispatcher.complete_with_tools(req)
    assert resp.model_used == "gpt-4o-mini-working"

    # 2. Check cooldown store: flaky model's tool_calls is suspended, but general model is active
    assert dispatcher._cooldowns.is_capability_active(("gpt-5-flaky", "fake"), "tool_calls") is True
    assert dispatcher._cooldowns.is_active(("gpt-5-flaky", "fake")) is False

    # 3. Plain completion should still pick and succeed on gpt-5-flaky (still available for chat)
    comp_candidates = dispatcher._candidates(ModelCapability.COMPLETION, PoolPriority.HIGH, 100)
    assert "gpt-5-flaky" in [c[0].model_id for c in comp_candidates]
    comp_req = CompletionRequest(prompt="hello", priority=PoolPriority.HIGH, component="test")
    comp_resp = await dispatcher.complete(comp_req)
    assert comp_resp.model_used == "gpt-5-flaky"

    # 4. Next complete_with_tools call automatically filters out flaky model upfront without retrying it
    candidates = dispatcher._candidates(ModelCapability.TOOL_CALLS, PoolPriority.HIGH, 100)
    candidate_ids = [c[0].model_id for c in candidates]
    assert "gpt-5-flaky" not in candidate_ids
    assert "gpt-4o-mini-working" in candidate_ids





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
async def test_openai_compat_detects_native_finish_network_error() -> None:
    sse_events = [
        "data: " + json.dumps({
            "choices": [{"delta": {"content": ""}, "finish_reason": "stop", "native_finish_reason": "network_error"}],
        }),
        "data: [DONE]",
    ]
    client = httpx.AsyncClient(transport=_MockStreamTransport(sse_events), base_url="http://test")
    provider = OpenAICompatibleProvider(name="openrouter", base_url="http://test", api_key="k")
    provider._client = client

    req = ToolCallRequest(
        messages=[{"role": "user", "content": "What is the weather?"}],
        tools=[{"name": "get_weather"}],
        component="test",
    )
    with pytest.raises(ModelDegenerateError) as exc_info:
        await provider.complete_with_tools("stealth/ox-alpha", req)
    assert "network_error" in exc_info.value.reason


@pytest.mark.asyncio
async def test_openai_compat_detects_empty_stream() -> None:
    sse_events = [
        "data: " + json.dumps({
            "choices": [{"delta": {"content": ""}}],
        }),
        "data: [DONE]",
    ]
    client = httpx.AsyncClient(transport=_MockStreamTransport(sse_events), base_url="http://test")
    provider = OpenAICompatibleProvider(name="openrouter", base_url="http://test", api_key="k")
    provider._client = client

    req = ToolCallRequest(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        component="test",
    )
    with pytest.raises(ModelDegenerateError) as exc_info:
        await provider.complete_with_tools("test-model", req)
    assert "empty stream" in exc_info.value.reason
