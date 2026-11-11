"""Claude's thinking: shown while it works, and kept across an agent's tool steps.

These run the real `anthropic` client against a fake of the API, so the SDK's own
stream parsing and typed models are exercised. The fake applies the API's rule for a
thinking block: its signature covers the model, system prompt, tools and every message
before it, and a block whose conversation changed is a 400.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import anthropic
import httpx2

from agents.reasoning import ReasoningStreamSplitter
from inference.models import ToolCallRequest
from inference.providers.anthropic_provider import AnthropicProvider, _cost_usd, _Usage

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]
_OTHER_TOOL = {**_TOOLS[0], "function": {**_TOOLS[0]["function"], "name": "list_dir"}}
_OPUS = "claude-opus-5-5"
_OLD = "claude-haiku-4-5"
_BOUND = (
    "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
    "different conversation. Remove the block, or set "
    '`thinking.block_binding.prefix_mismatch_behavior` to "drop_block".'
)


def _sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class _FakeClaude:
    """The Messages and Models endpoints, with the signature check."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.refuse_next = False
        self.calls = 0

    @staticmethod
    def _signature(body: dict, upto: int) -> str:
        system = "".join(part["text"] for part in body.get("system", []))
        record = [body["model"], system, body.get("tools"), body["messages"][:upto]]
        return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()

    def _bad_signature(self, body: dict) -> bool:
        for index, message in enumerate(body["messages"]):
            if message["role"] != "assistant" or not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                if block["type"] == "thinking" and block["signature"] != self._signature(body, index):
                    return True
        return False

    @staticmethod
    def _models() -> httpx2.Response:
        def model(model_id: str, adaptive: bool) -> dict:
            thinking = {
                "supported": True,
                "types": {"enabled": {"supported": not adaptive}, "adaptive": {"supported": adaptive}},
            }
            return {
                "id": model_id,
                "type": "model",
                "display_name": model_id,
                "created_at": "2026-01-01T00:00:00Z",
                "max_input_tokens": 1_000_000,
                "max_tokens": 128_000,
                "capabilities": {"thinking": thinking},
            }

        data = [model(_OPUS, True), model(_OLD, False)]
        return httpx2.Response(200, json={"data": data, "has_more": False, "first_id": _OPUS, "last_id": _OLD})

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return self._models()
        body = json.loads(request.content)
        self.requests.append(body)
        if self._bad_signature(body) or (self.refuse_next and self._sends_thinking(body)):
            self.refuse_next = False
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": _BOUND}}
            return httpx2.Response(400, json=error)
        self.calls += 1
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=self._turn(body))

    @staticmethod
    def _sends_thinking(body: dict) -> bool:
        return any(
            isinstance(m["content"], list) and any(b["type"] == "thinking" for b in m["content"])
            for m in body["messages"]
            if m["role"] == "assistant"
        )

    def _turn(self, body: dict) -> bytes:
        usage = {"input_tokens": 10, "output_tokens": 0}
        call_id = f"toolu_{self.calls}"
        events: list[dict] = [
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{self.calls}",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": usage,
                },
            }
        ]
        index = 0
        if body.get("thinking"):
            signature = self._signature(body, len(body["messages"]))
            events += [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "Check the "},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "notes first."},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": signature},
                },
                {"type": "content_block_stop", "index": 0},
            ]
            index = 1
        events += [
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": call_id, "name": "read_file", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.md"}'},
            },
            {"type": "content_block_stop", "index": index},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        return _sse(*events)


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


def _start() -> list[dict]:
    return [{"role": "system", "content": "You are north."}, {"role": "user", "content": "read a.md"}]


async def _step(
    provider: AnthropicProvider,
    history: list[dict],
    *,
    model: str = _OPUS,
    tools: list[dict] | None = None,
    on_token=None,
) -> Any:
    """One agent step; appends what the agent loop appends for a tool call."""
    response = await provider.complete_with_tools(
        model, ToolCallRequest(messages=history, tools=tools or _TOOLS, component="general"), token_callback=on_token
    )
    call = response.calls[0]
    history += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call.call_id, "function": {"name": call.name, "arguments": json.dumps(call.params)}}],
        },
        {"role": "tool", "tool_call_id": call.call_id, "content": "file text"},
    ]
    return response


def _thinking_sent(body: dict) -> int:
    return sum(
        block["type"] == "thinking"
        for message in body["messages"]
        if message["role"] == "assistant" and isinstance(message["content"], list)
        for block in message["content"]
    )


def test_fable_5_2_and_mythos_5_2_are_priced() -> None:
    usage = _Usage(tokens_in=1_000_000, tokens_out=1_000_000, cached_tokens=0, cache_write_tokens=0)
    assert _cost_usd(usage, "claude-fable-5-2") == 48.0
    assert _cost_usd(usage, "claude-mythos-5-2") == 48.0


def test_sonnet_5_5_cache_reads_cost_ten_cents_a_million() -> None:
    usage = _Usage(tokens_in=1_000_000, tokens_out=0, cached_tokens=1_000_000, cache_write_tokens=0)
    assert _cost_usd(usage, "claude-sonnet-5-5") == 0.10


async def test_thinking_is_asked_for_only_on_a_model_that_takes_it() -> None:
    provider, fake = await _provider()
    await _step(provider, _start())
    await _step(provider, _start(), model=_OLD)
    assert fake.requests[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "thinking" not in fake.requests[1]


async def test_thinking_streams_as_a_thought_and_comes_back_on_the_response() -> None:
    provider, _ = await _provider()
    tokens: list[str] = []

    async def on_token(token: str) -> None:
        tokens.append(token)

    response = await _step(provider, _start(), on_token=on_token)

    assert response.reasoning == "Check the notes first."
    splitter = ReasoningStreamSplitter()
    split = [part for token in tokens for part in splitter.feed(token)]
    assert "".join(text for channel, text in split if channel == "reasoning") == "Check the notes first."
    assert not [text for channel, text in split if channel == "answer"]


async def test_each_step_sends_back_the_thinking_of_the_steps_before() -> None:
    provider, fake = await _provider()
    history = _start()
    for _ in range(3):
        await _step(provider, history)
    assert [_thinking_sent(body) for body in fake.requests] == [0, 1, 2]
    assert len(fake.requests) == fake.calls == 3


async def test_the_thinking_goes_back_before_its_tool_call_with_its_signature() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    await _step(provider, history)
    turn = fake.requests[1]["messages"][1]
    assert [block["type"] for block in turn["content"]] == ["thinking", "tool_use"]
    assert turn["content"][0]["signature"]


async def test_compacting_history_drops_the_thinking_instead_of_failing() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    await _step(provider, history)
    history[1]["content"] = "Summary of the work so far."  # what compaction does to the start

    await _step(provider, history)

    assert _thinking_sent(fake.requests[-1]) == 0
    assert len(fake.requests) == fake.calls == 3  # never refused: north dropped it first


async def test_steps_after_compaction_keep_their_new_thinking() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    history[1]["content"] = "Summary."
    await _step(provider, history)
    await _step(provider, history)
    assert _thinking_sent(fake.requests[-1]) == 1  # the step after compaction, not the one before


async def test_a_changed_tool_list_drops_the_thinking_instead_of_failing() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    await _step(provider, history, tools=[*_TOOLS, _OTHER_TOOL])
    assert _thinking_sent(fake.requests[-1]) == 0
    assert len(fake.requests) == fake.calls == 2  # never refused: north dropped it first


async def test_another_models_thinking_is_not_sent() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    await _step(provider, history, model=_OLD)
    assert _thinking_sent(fake.requests[-1]) == 0
    assert len(fake.requests) == fake.calls == 2  # never refused: north left it out


async def test_a_refused_thinking_block_is_dropped_and_the_step_still_runs() -> None:
    provider, fake = await _provider()
    history = _start()
    await _step(provider, history)
    fake.refuse_next = True

    await _step(provider, history)

    assert [_thinking_sent(body) for body in fake.requests] == [0, 1, 0]
    assert fake.calls == 2
    await _step(provider, history)
    assert _thinking_sent(fake.requests[-1]) == 1  # only what came after the refusal


async def test_another_bad_request_is_not_retried() -> None:
    provider, fake = await _provider()

    def refuse(_: httpx2.Request) -> httpx2.Response:
        error = {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens is too large"}}
        return httpx2.Response(400, json=error)

    provider._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(refuse)),
    )
    try:
        await _step(provider, _start())
    except Exception as exc:
        assert "max_tokens is too large" in str(exc)
    else:
        raise AssertionError("expected the request to fail")
    assert fake.calls == 0
