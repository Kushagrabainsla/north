"""Mistral: its own models and the open-weight models it hosts (GLM, DeepSeek), through one key.

The shapes below are the ones Mistral's API returned on 2026-11-09: a model list that repeats a model
under each alias and carries no price, and a reasoning model's answer sent as a list of parts,
which the OpenAI-compatible base read only as a string.
"""

from __future__ import annotations

import json

import httpx
import pytest

from inference.capability import ModelCapability as Cap
from inference.models import CompletionRequest, ToolCallRequest
from inference.providers.mistral import PRICES, MistralRouter, cost_usd
from inference.registry import PROVIDER_DEFINITIONS


def _model(model_id: str, name: str, billing: str, **flags: bool) -> dict:
    return {
        "id": model_id,
        "name": name,
        "billing_model_name": billing,
        "max_context_length": 1_048_576,
        "capabilities": {"completion_chat": True, "function_calling": True, **flags},
    }


_MODELS = [
    _model("zai-glm-5-3", "zai-glm-5-3", "zai-glm-5-3", reasoning=True),
    _model("zai-glm-5", "zai-glm-5-3", "zai-glm-5-3", reasoning=True),
    _model("zai-glm-latest", "zai-glm-5-3", "zai-glm-5-3", reasoning=True),
    _model("mistral-medium-2604", "mistral-medium-latest", "mistral-medium-3-5", vision=True),
    _model("mistral-medium-latest", "mistral-medium-latest", "mistral-medium-3-5", vision=True),
    _model("mistral-large-2512", "mistral-large-2512", "mistral-large-2512", vision=True),
    _model("voxtral-small-2507", "Voxtral Small Transcribe", "voxtral-small-2507"),
    _model("labs-leanstral-1-5-1", "labs-leanstral-1-5-1", "labs-leanstral-1-5"),
    {"id": "codestral-embed", "name": "codestral-embed", "billing_model_name": "codestral-embed", "capabilities": {}},
]


def _sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"


async def _router(handler) -> MistralRouter:
    router = MistralRouter("key")
    router._client = httpx.AsyncClient(base_url="https://api.mistral.ai/v1", transport=httpx.MockTransport(handler))
    return router


def _listing(request: httpx.Request) -> httpx.Response | None:
    return (
        httpx.Response(200, json={"object": "list", "data": _MODELS}) if request.url.path.endswith("/models") else None
    )


async def test_each_model_is_listed_once_with_what_mistral_says_it_can_do() -> None:
    router = await _router(_listing)
    await router.refresh()
    models = router.get_models()

    assert set(models) == {
        "zai-glm-5-3",
        "mistral-medium-latest",
        "mistral-large-2512",
    }  # aliases folded, audio and labs out
    glm, medium = models["zai-glm-5-3"], models["mistral-medium-latest"]
    assert {Cap.COMPLETION, Cap.TOOL_CALLS, Cap.REASONING} <= glm.capabilities and Cap.VISION not in glm.capabilities
    assert Cap.VISION in medium.capabilities and glm.context_window == 1_048_576
    assert glm.cost_per_token == pytest.approx(4.40 / 1_000_000) and glm.price_known
    assert not models["mistral-large-2512"].price_known  # no published price: ranked by a stand-in, never shown as one


async def test_a_reasoning_models_answer_in_parts_is_split_into_text_and_reasoning_and_priced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (listed := _listing(request)) is not None:
            return listed
        thinking = {"type": "thinking", "thinking": [{"type": "text", "text": "Five words, then."}]}
        return httpx.Response(
            200,
            json={
                "model": "zai-glm-5-3",
                "choices": [
                    {"message": {"content": [thinking, {"type": "text", "text": "Hello there, how are you?"}]}}
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 400},
                },
            },
        )

    router = await _router(handler)
    await router.refresh()
    answer = await router.complete(
        "zai-glm-latest", CompletionRequest(prompt="Say hello in five words.", component="t")
    )

    assert (answer.text, answer.reasoning) == ("Hello there, how are you?", "Five words, then.")
    assert answer.priced and answer.cost_usd == pytest.approx((600 * 1.40 + 400 * 0.14 + 200 * 4.40) / 1_000_000)


async def test_a_streamed_tool_call_with_thinking_parts_comes_through_whole_and_priced() -> None:
    """A streamed list of thinking parts raised TypeError when north joined the pieces of the answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if (listed := _listing(request)) is not None:
            return listed
        thought = [{"type": "thinking", "thinking": [{"type": "text", "text": "Use the tool."}], "closed": True}]
        call = {"id": "chatcmpl-tool-1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
                {"choices": [{"index": 0, "delta": {"content": thought}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [call | {"index": 0}]}}]},
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": "Paris"}'}}]},
                        }
                    ]
                },
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {
                        "prompt_tokens": 164,
                        "completion_tokens": 40,
                        "prompt_tokens_details": {"cached_tokens": 0},
                    },
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    router = await _router(handler)
    await router.refresh()
    tools = [{"name": "get_weather", "description": "Weather", "parameters": {"type": "object", "properties": {}}}]
    request = ToolCallRequest(messages=[{"role": "user", "content": "Weather in Paris?"}], tools=tools, component="t")
    turn = await router.complete_with_tools("zai-glm-5-3", request)

    assert [(call.name, call.params) for call in turn.calls] == [("get_weather", {"city": "Paris"})]
    assert turn.reasoning == "Use the tool." and turn.priced
    assert turn.cost_usd == pytest.approx((164 * 1.40 + 40 * 4.40) / 1_000_000)


async def test_a_call_to_a_model_mistral_published_no_price_for_is_recorded_as_unpriced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (listed := _listing(request)) is not None:
            return listed
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
        )

    router = await _router(handler)
    await router.refresh()
    answer = await router.complete("mistral-large-2512", CompletionRequest(prompt="hi", component="t"))

    assert (answer.priced, answer.cost_usd) == (False, 0.0)


async def test_a_key_mistral_refuses_says_so_when_the_models_are_listed() -> None:
    from inference.exceptions import PoolRefreshError

    router = await _router(lambda request: httpx.Response(401, json={"message": "Unauthorized"}))

    with pytest.raises(PoolRefreshError, match="401"):
        await router.refresh()


def test_cached_input_with_no_published_rate_is_charged_as_input() -> None:
    assert cost_usd(PRICES["zai-glm-5-3-flash"], 1000, 400, 0) == pytest.approx(1000 * 0.15 / 1_000_000)


def test_mistral_is_a_provider_north_offers_with_a_console_key() -> None:
    [mistral] = [definition for definition in PROVIDER_DEFINITIONS if definition.id == "mistral"]

    assert (mistral.env_key, mistral.settings_field) == ("NORTH_MISTRAL_API_KEY", "mistral_api_key")
    assert mistral.setup_url == "https://console.mistral.ai/api-keys"
    assert isinstance(mistral.build("key"), MistralRouter)
