"""Retiring an old tool result summarises it instead of cutting it.

Issue #10 measured head-truncation as north's weakest context handling: 0.66
against summarisation's 1.00, and 0.00 on both 64k tasks, because the answer
sits in the middle of a long output far more often than at its start. Spilling
the remainder to `read_tool_output` made the answer *recoverable*; it did not
make it read, because recovery depends on the agent choosing to go back for it.

These assert the summary is in front of the agent, the route back to the
original survives, and that nothing here fails closed when there is no model.
"""

from __future__ import annotations

import json

import pytest

from agents.context_compaction import compact_history_with_summaries, summarise_tool_messages
from tools.output_spill import spill_store

NEEDLE = "THE-ANSWER-IS-42"


@pytest.fixture(autouse=True)
def _clean_store():
    spill_store().clear()
    yield
    spill_store().clear()


def _text_with_needle_in_middle(total: int) -> str:
    """Filler with the needle buried at the midpoint - never in the head or tail."""
    half = "x" * (total // 2)
    return half + NEEDLE + half


class _Router:
    """A router that summarises by reporting whether it saw the needle."""

    def __init__(self, text: str = f"Found {NEEDLE} in the output.") -> None:
        self.text = text
        self.prompts: list[str] = []

    async def complete(self, request):
        self.prompts.append(request.prompt)
        return type("Resp", (), {"text": self.text})()


class _FailingRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        raise RuntimeError("no model could serve this")


def _history(body: str, pairs: int = 4, task: str = "find the answer") -> list[dict]:
    messages: list[dict] = [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": task},
    ]
    for n in range(pairs):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{n}", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": body})
    return messages


# ── The summary replaces the output ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_retired_result_is_summarised_not_cut() -> None:
    """The 64k case that scored 0.00: the answer is in the middle."""
    body = _text_with_needle_in_middle(64_000)
    messages = _history(body)
    router = _Router()

    count = await compact_history_with_summaries(messages, keep_recent=2, inference_router=router)

    assert count == 2, "both retired results should be summarised"
    retired = messages[3]["content"]
    assert NEEDLE in retired, "the answer must survive in the summary, not only in the spill store"
    assert len(retired) < len(body), "the window must still shrink"


@pytest.mark.asyncio
async def test_the_summary_is_written_against_the_task() -> None:
    """A summary produced without the task keeps what looks important, not what is needed."""
    messages = _history("y" * 4_000, task="what timeout does the config set?")
    router = _Router()

    await summarise_tool_messages(messages, [3], inference_router=router)

    assert "what timeout does the config set?" in router.prompts[0]
    assert "read_file" in router.prompts[0]


@pytest.mark.asyncio
async def test_the_original_is_still_reachable_after_summarising() -> None:
    """A summary is lossy, so the handle back to the full text must survive."""
    body = _text_with_needle_in_middle(20_000)
    messages = _history(body)

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=_Router("a summary"))

    retired = messages[3]["content"]
    handle = retired.split("handle '")[1].split("'")[0]
    matches = spill_store().find(handle, NEEDLE)
    assert matches and NEEDLE in matches[0].excerpt


@pytest.mark.asyncio
async def test_a_json_result_keeps_its_success_and_error_keys() -> None:
    """The agent reads these to know whether the call worked; a summary must not hide them."""
    body = json.dumps({"success": False, "error": "boom", "data": {"content": "z" * 4_000}})
    messages = _history(body)

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=_Router("it failed"))

    retired = json.loads(messages[3]["content"])
    assert retired["success"] is False
    assert retired["error"] == "boom"
    assert retired["summary"] == "it failed"
    assert "_handle" in retired


# ── Degrading ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_router_falls_back_to_cutting() -> None:
    """Compaction must work with no model at all - tests, offline, exhausted tier."""
    body = _text_with_needle_in_middle(20_000)
    messages = _history(body)

    count = await compact_history_with_summaries(messages, keep_recent=2, inference_router=None)

    assert count == 0
    retired = messages[3]["content"]
    assert NEEDLE not in retired
    assert "read_tool_output" in retired, "the fallback must still say how to get the rest"


@pytest.mark.asyncio
async def test_a_failed_summary_falls_back_to_cutting_that_result() -> None:
    """One model failure must not leave a full-size result in the window."""
    body = "q" * 20_000
    messages = _history(body)
    router = _FailingRouter()

    count = await compact_history_with_summaries(messages, keep_recent=2, inference_router=router)

    assert count == 0
    assert router.calls == 2
    assert len(messages[3]["content"]) < 2_000, "it must still shrink when the summary fails"
    assert "read_tool_output" in messages[3]["content"]


# ── Cost ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_result_is_summarised_at_most_once() -> None:
    """Compaction runs every iteration; paying per run rather than per result would be ruinous."""
    messages = _history(_text_with_needle_in_middle(20_000))
    router = _Router()

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=router)
    calls_after_first = len(router.prompts)
    await compact_history_with_summaries(messages, keep_recent=2, inference_router=router)

    assert calls_after_first == 2
    assert len(router.prompts) == 2, "a second pass must not re-summarise what it already summarised"


@pytest.mark.asyncio
async def test_reshrinking_does_not_evict_the_original_it_points_at() -> None:
    """The marker can push a shrunken result back over the threshold.

    Without the already-shrunk guard the next pass spills the truncated copy as a
    new entry, and the handle in the window then points at text that no longer
    holds the answer.
    """
    messages = _history(_text_with_needle_in_middle(20_000))
    await compact_history_with_summaries(messages, keep_recent=2, inference_router=None)
    retired = messages[3]["content"]
    handle = retired.split("handle '")[1].split("'")[0]

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=None)

    assert messages[3]["content"] == retired, "an already-shrunk result must be left alone"
    matches = spill_store().find(handle, NEEDLE)
    assert matches, "the original must still be behind its handle"


@pytest.mark.asyncio
async def test_results_still_in_play_are_untouched() -> None:
    """Only retired results are summarised; the recent ones are what the agent is working on."""
    body = _text_with_needle_in_middle(20_000)
    messages = _history(body)

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=_Router())

    assert messages[-1]["content"] == body


@pytest.mark.asyncio
async def test_a_short_result_is_not_worth_a_model_call() -> None:
    messages = _history("small output")
    router = _Router()

    await compact_history_with_summaries(messages, keep_recent=2, inference_router=router)

    assert router.prompts == []
