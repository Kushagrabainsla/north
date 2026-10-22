"""A conversation is sent append-only: a note added mid-run never changes what came before it."""

import json

import httpx
import pytest

from inference.models import ToolCallRequest
from inference.providers.openai_compat import OpenAICompatibleProvider
from inference.transcript import message_text, system_notes_in_place

_NOTE = "Soft efficiency budget reached."
_RUN = [
    {"role": "system", "content": "You are north."},
    {"role": "user", "content": "Find the file."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "glob", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
    {"role": "system", "content": _NOTE},
    {"role": "assistant", "content": "a.py"},
]


def test_the_opening_system_message_stays_the_system_prompt() -> None:
    assert system_notes_in_place(_RUN)[0] == {"role": "system", "content": "You are north."}


@pytest.mark.parametrize("role", ["system", "developer"])
def test_a_system_note_mid_run_is_sent_where_it_arrived_as_a_user_note(role: str) -> None:
    run = [*_RUN[:4], {"role": role, "content": _NOTE}]

    assert system_notes_in_place(run) == [*_RUN[:4], {"role": "user", "content": f"[System note] {_NOTE}"}]


def test_every_call_starts_with_the_previous_call_unchanged() -> None:
    """The property the provider cache needs: each sent transcript extends the one before."""
    sent = [system_notes_in_place(_RUN[:end]) for end in range(1, len(_RUN) + 1)]

    for before, after in zip(sent, sent[1:], strict=False):
        assert after[: len(before)] == before


def test_message_text_joins_the_text_parts() -> None:
    parts = [
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "b"},
    ]

    assert message_text(parts) == "a\nb"
    assert message_text(None) == ""


@pytest.mark.asyncio
async def test_an_openai_compatible_provider_sends_a_mid_run_note_in_place() -> None:
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    provider = OpenAICompatibleProvider(name="test", base_url="https://example.test", api_key="k")
    provider._client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))  # noqa: SLF001
    await provider.complete_with_tools("m", ToolCallRequest(messages=_RUN[:5], tools=[], component="test"))
    await provider._client.aclose()  # noqa: SLF001

    assert sent[0]["messages"] == [*_RUN[:4], {"role": "user", "content": f"[System note] {_NOTE}"}]
