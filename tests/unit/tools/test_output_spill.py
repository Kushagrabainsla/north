"""Tests for the tool-output overflow store and read_tool_output.

The behaviour under test is the one issue #10 measured as north's weakest
context handling: an oversized tool result used to keep its head and drop the
rest, so an answer in the middle was gone and the agent could not tell. These
assert the middle is now reachable.
"""

from __future__ import annotations

import json

import pytest

from agents.agentic_llm_agent import _cap_tool_result
from agents.constants import MAX_TOOL_RESULT_CHARS
from agents.context_compaction import compact_history
from tools.models import ToolInput
from tools.output_spill import DEFAULT_READ_CHARS, MAX_READ_CHARS, OutputSpillStore, spill_store
from tools.universal.read_tool_output import ReadToolOutputTool

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


# ── The store ────────────────────────────────────────────────────────────────


def test_read_returns_a_window_and_the_next_offset() -> None:
    store = OutputSpillStore()
    handle = store.store("grep", "abcdefghij")

    first = store.read(handle, offset=0, limit=4)
    assert first is not None
    assert first.text == "abcd"
    assert first.total_chars == 10
    assert first.next_offset == 4

    last = store.read(handle, offset=8, limit=4)
    assert last is not None
    assert last.text == "ij"
    assert last.next_offset is None


def test_read_clamps_offset_and_limit() -> None:
    store = OutputSpillStore()
    handle = store.store("grep", "abc")

    assert store.read(handle, offset=999).text == ""
    assert store.read(handle, offset=-5).offset == 0
    assert store.read(handle, limit=MAX_READ_CHARS * 10).returned_chars == 3


def test_find_locates_a_needle_the_head_would_have_missed() -> None:
    store = OutputSpillStore()
    handle = store.store("read_file", _text_with_needle_in_middle(60_000))

    matches = store.find(handle, NEEDLE)

    assert matches is not None and len(matches) == 1
    assert NEEDLE in matches[0].excerpt
    assert matches[0].offset == 30_000


def test_find_returns_empty_when_absent_and_none_when_handle_unknown() -> None:
    store = OutputSpillStore()
    handle = store.store("read_file", "nothing to see")

    assert store.find(handle, "absent") == []
    assert store.find("tool_output:missing", "absent") is None
    assert store.read("tool_output:missing") is None


def test_eviction_is_bounded_by_count_and_by_size() -> None:
    by_count = OutputSpillStore(max_entries=2, max_total_chars=10_000)
    first = by_count.store("t", "a")
    by_count.store("t", "b")
    by_count.store("t", "c")
    assert by_count.read(first) is None, "oldest entry should be evicted once the count cap is passed"

    by_size = OutputSpillStore(max_entries=100, max_total_chars=10)
    oldest = by_size.store("t", "x" * 8)
    newest = by_size.store("t", "y" * 8)
    assert by_size.read(oldest) is None
    assert by_size.read(newest) is not None


def test_reading_an_entry_protects_it_from_eviction() -> None:
    store = OutputSpillStore(max_entries=2, max_total_chars=10_000)
    first = store.store("t", "a")
    store.store("t", "b")

    store.read(first)  # most-recently-used is now `first`
    store.store("t", "c")

    assert store.read(first) is not None, "a recently read entry must not be the one evicted"


# ── Fresh results: the cap that used to lose the middle ──────────────────────


def test_oversized_result_stays_within_the_cap() -> None:
    data = {"success": True, "data": {"content": "y" * (MAX_TOOL_RESULT_CHARS * 2)}}

    raw = _cap_tool_result(data, "read_file")

    assert len(raw) <= MAX_TOOL_RESULT_CHARS
    assert json.loads(raw), "the result handed to the model must stay valid JSON"


def test_the_middle_of_an_oversized_result_is_recoverable() -> None:
    body = _text_with_needle_in_middle(MAX_TOOL_RESULT_CHARS * 2)
    data = {"success": True, "data": {"content": body}}

    raw = _cap_tool_result(data, "read_file")
    payload = json.loads(raw)

    assert NEEDLE not in raw, "the needle is in the middle, so the capped result must not contain it"
    matches = spill_store().find(payload["_handle"], NEEDLE)
    assert matches and NEEDLE in matches[0].excerpt


def test_the_note_says_how_much_is_missing_and_how_to_get_it() -> None:
    data = {"success": True, "data": {"content": "y" * (MAX_TOOL_RESULT_CHARS * 2)}}

    payload = json.loads(_cap_tool_result(data, "read_file"))

    note = payload["_note"]
    assert "read_tool_output" in note, "the agent must be told the call that recovers the rest"
    assert payload["_handle"] in note
    assert "not shown" in note


def test_a_result_that_fits_is_untouched() -> None:
    data = {"success": True, "data": {"content": "small"}}

    payload = json.loads(_cap_tool_result(data, "read_file"))

    assert payload == data
    assert "_handle" not in payload


def test_non_string_overflow_still_stays_within_the_cap() -> None:
    """The fallback branch: a huge list cannot be shortened field by field."""
    data = {"success": True, "data": {"rows": [{"i": i, "pad": "z" * 100} for i in range(5_000)]}}

    raw = _cap_tool_result(data, "search_files")

    assert len(raw) <= MAX_TOOL_RESULT_CHARS
    payload = json.loads(raw)
    assert spill_store().read(payload["_handle"]) is not None


# ── History compaction: retiring an old result is now reversible ─────────────


def test_compacting_history_keeps_the_retired_output_reachable() -> None:
    """The oldest tool result is shrunk out of the window but stays fetchable."""
    body = json.dumps({"success": True, "data": {"content": _text_with_needle_in_middle(20_000)}})
    messages: list[dict] = [{"role": "user", "content": "find it"}]
    for n in range(4):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{n}", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": body})

    compact_history(messages, keep_recent=2)

    oldest = messages[2]["content"]
    assert NEEDLE not in oldest, "the retired result must no longer carry its body in the window"
    matches = spill_store().find(json.loads(oldest)["_handle"], NEEDLE)
    assert matches and NEEDLE in matches[0].excerpt


def test_compacting_history_leaves_recent_results_intact() -> None:
    """Only retired results are shrunk - the ones still in play are untouched."""
    body = json.dumps({"success": True, "data": {"content": _text_with_needle_in_middle(20_000)}})
    messages: list[dict] = [{"role": "user", "content": "find it"}]
    for n in range(4):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{n}", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": body})

    compact_history(messages, keep_recent=2)

    assert NEEDLE in messages[-1]["content"]


# ── The tool ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_find_returns_the_matching_section() -> None:
    handle = spill_store().store("read_file", _text_with_needle_in_middle(60_000))

    result = await ReadToolOutputTool().run(ToolInput(params={"handle": handle, "pattern": NEEDLE}))

    assert result.success
    assert result.data["matches"][0]["offset"] == 30_000
    assert NEEDLE in result.data["matches"][0]["excerpt"]


@pytest.mark.asyncio
async def test_tool_defaults_to_find_when_given_a_pattern() -> None:
    handle = spill_store().store("read_file", f"head {NEEDLE} tail")

    result = await ReadToolOutputTool().run(ToolInput(params={"handle": handle, "pattern": NEEDLE}))

    assert result.data["action"] == "find"


@pytest.mark.asyncio
async def test_tool_read_pages_through_with_next_offset() -> None:
    handle = spill_store().store("read_file", "abcdefghij")
    tool = ReadToolOutputTool()

    first = await tool.run(ToolInput(params={"handle": handle, "action": "read", "limit": 4}))
    assert first.data["text"] == "abcd"

    second = await tool.run(
        ToolInput(params={"handle": handle, "action": "read", "offset": first.data["next_offset"], "limit": 4})
    )
    assert second.data["text"] == "efgh"


@pytest.mark.asyncio
async def test_tool_accepts_string_numbers_from_the_model() -> None:
    handle = spill_store().store("read_file", "abcdefghij")

    result = await ReadToolOutputTool().run(
        ToolInput(params={"handle": handle, "action": "read", "offset": "4", "limit": "3"})
    )

    assert result.data["text"] == "efg"


@pytest.mark.asyncio
async def test_tool_reports_an_expired_handle_without_pretending_it_was_empty() -> None:
    result = await ReadToolOutputTool().run(ToolInput(params={"handle": "tool_output:gone", "pattern": "x"}))

    assert not result.success
    assert "Re-run the original tool" in result.error


@pytest.mark.asyncio
async def test_tool_rejects_a_bad_regex_rather_than_raising() -> None:
    handle = spill_store().store("read_file", "text")

    result = await ReadToolOutputTool().run(ToolInput(params={"handle": handle, "pattern": "([unclosed"}))

    assert not result.success
    assert "Invalid regular expression" in result.error


@pytest.mark.asyncio
async def test_tool_requires_a_handle() -> None:
    result = await ReadToolOutputTool().run(ToolInput(params={"pattern": "x"}))

    assert not result.success
    assert "handle" in result.error


def test_no_match_is_reported_as_a_complete_search() -> None:
    """A search that found nothing must not read as another truncation."""
    rendered = ReadToolOutputTool().format_output(
        {"action": "find", "pattern": "absent", "total_chars": 100, "matches": []}
    )

    assert "complete search" in rendered


def test_default_read_limit_is_below_the_result_cap() -> None:
    """Paging must not be able to re-create the blowout that caused the spill."""
    assert DEFAULT_READ_CHARS < MAX_TOOL_RESULT_CHARS
    assert MAX_READ_CHARS <= MAX_TOOL_RESULT_CHARS
