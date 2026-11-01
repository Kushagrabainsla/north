"""An agent run's steps after the first read their prompt from Anthropic's cache.

Every step resends the tools, the system prompt and the history. North sent no
cache marker, so in the issue #76 test 851 calls read 0 tokens from cache and
each step of a trivial task paid for 27.6k input tokens again.

What makes a read possible is checked here: both markers are sent, and the
second step's prompt starts with exactly the first step's tools, system prompt
and messages. That a real second step reads from the cache was measured against
the API: see the commit that added this test.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from inference.models import ToolCallRequest
from inference.providers.anthropic_provider import AnthropicProvider

_SYSTEM = "You are north's general agent. " * 200
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


class _Stream:
    def __init__(self, message: Any) -> None:
        self._message = message

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> Any:
        return self._message


class _Messages:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _Stream:
        self.sent.append(copy.deepcopy(kwargs))
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        block = SimpleNamespace(type="text", text="done")
        return _Stream(SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn", stop_details=None))


def _provider() -> tuple[AnthropicProvider, _Messages]:
    provider = AnthropicProvider(api_key="test-key")
    messages = _Messages()
    provider._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return provider, messages


async def _step(provider: AnthropicProvider, history: list[dict]) -> None:
    await provider.complete_with_tools(
        "claude-haiku-5-5", ToolCallRequest(messages=history, tools=_TOOLS, component="general")
    )


async def test_a_tool_call_marks_the_system_prompt_and_the_conversation_for_caching() -> None:
    provider, sent = _provider()

    await _step(provider, [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "17 x 23?"}])

    request = sent.sent[0]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["system"] == [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]


async def test_the_next_step_starts_with_the_previous_steps_prompt() -> None:
    provider, sent = _provider()
    history: list[dict] = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "read notes.md"}]
    await _step(provider, history)
    history += [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "notes.md"}'}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "buy milk"},
    ]

    await _step(provider, history)

    first, second = sent.sent
    assert second["tools"] == first["tools"]
    assert second["system"] == first["system"]
    assert second["messages"][: len(first["messages"])] == first["messages"]
