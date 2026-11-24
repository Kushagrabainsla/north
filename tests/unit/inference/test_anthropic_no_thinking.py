"""A completion that asks for no thinking: thinking off where the model allows it, and low effort.

Haiku 5.5 thinks by default. About 530 of the 646 output tokens of each approval were thinking, 2.7 s of
3.5 s (2026-11-23). Opus 5.5 and Sonnet 5.5 reject thinking off with a 400, so they get low effort alone.
What each model allows comes from the Models API, as its raw capabilities say it.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from approval.decider import MemoryDecider
from approval.policy import Action, ActionKind
from inference.models import CompletionRequest, CompletionResponse
from inference.providers.anthropic_provider import AnthropicProvider

_HAIKU = "claude-haiku-5-5"
_OPUS = "claude-opus-5-5"
_OLD = "claude-haiku-4-5"


def _model(model_id: str, *, disabled: bool, effort: bool) -> dict:
    types = {"enabled": {"supported": False}, "adaptive": {"supported": True}, "disabled": {"supported": disabled}}
    capabilities: dict = {"thinking": {"supported": True, "types": types}}
    if effort:
        levels = {level: {"supported": True} for level in ("low", "medium", "high", "max")}
        capabilities["effort"] = {"supported": True, **levels}
    return {
        "id": model_id,
        "type": "model",
        "display_name": model_id,
        "created_at": "2026-01-01T00:00:00Z",
        "max_input_tokens": 1_000_000,
        "max_tokens": 128_000,
        "capabilities": capabilities,
    }


class _FakeClaude:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            data = [
                _model(_HAIKU, disabled=True, effort=True),
                _model(_OPUS, disabled=False, effort=True),
                {**_model(_OLD, disabled=False, effort=False), "capabilities": None},
            ]
            return httpx2.Response(200, json={"data": data, "has_more": False, "first_id": _HAIKU, "last_id": _OLD})
        body = json.loads(request.content)
        self.bodies.append(body)
        message = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": '{"decision": "approve"}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        return httpx2.Response(200, json=message)


async def _provider() -> tuple[AnthropicProvider, _FakeClaude]:
    fake = _FakeClaude()
    provider = AnthropicProvider(api_key="test-key")
    provider._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handle)),
    )
    await provider.refresh()
    return provider, fake


def _request(**fields) -> CompletionRequest:
    return CompletionRequest(prompt="Rule on this.", component="approval_decider", json_mode=True, **fields)


async def test_a_model_that_allows_it_gets_thinking_off_and_low_effort() -> None:
    provider, fake = await _provider()

    await provider.complete(_HAIKU, _request(thinking=False))

    assert fake.bodies[0]["thinking"] == {"type": "disabled"}
    assert fake.bodies[0]["output_config"] == {"effort": "low"}


async def test_a_model_that_rejects_thinking_off_gets_low_effort_alone() -> None:
    provider, fake = await _provider()

    await provider.complete(_OPUS, _request(thinking=False))

    assert "thinking" not in fake.bodies[0]
    assert fake.bodies[0]["output_config"] == {"effort": "low"}


async def test_a_model_that_says_nothing_gets_nothing() -> None:
    provider, fake = await _provider()

    await provider.complete(_OLD, _request(thinking=False))

    assert "thinking" not in fake.bodies[0] and "output_config" not in fake.bodies[0]


async def test_a_request_that_does_not_ask_keeps_the_model_default() -> None:
    provider, fake = await _provider()

    await provider.complete(_HAIKU, _request())

    assert "thinking" not in fake.bodies[0] and "output_config" not in fake.bodies[0]


async def test_low_effort_joins_a_structured_output_and_does_not_replace_it() -> None:
    provider, fake = await _provider()
    schema = {"type": "object", "properties": {"decision": {"type": "string"}}}

    await provider.complete(_HAIKU, _request(thinking=False, response_schema=schema))

    config = fake.bodies[0]["output_config"]
    assert config["effort"] == "low" and config["format"]["type"] == "json_schema"


class _Router:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        reply = '{"decision": "approve", "reason": "the user asked", "used": []}'
        return CompletionResponse(text=reply, model_used=_HAIKU, cost_usd=0.0, tokens_in=1, tokens_out=1)


class _Memory:
    async def principal_for(self, component):
        return None

    async def recall(self, principal, query, **limits):
        class _Recalled:
            facts: list[str] = []
            episodes: list[str] = []
            documents: list[str] = []

        return _Recalled()

    async def read_document(self, document):
        return ""


@pytest.mark.asyncio
async def test_the_decider_asks_for_no_thinking() -> None:
    router = _Router()
    decider = MemoryDecider(memory=_Memory(), inference_router=router)

    await decider.rule(Action(agent="browser", kind=ActionKind.BROWSER, operation="click", summary="Click Search"))

    assert [request.thinking for request in router.requests] == [False]
