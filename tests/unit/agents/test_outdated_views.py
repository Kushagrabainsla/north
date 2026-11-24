"""Only a tool's newest view of a page goes back to the model; older views are cut out of its results.

By turn 74 of one live run (2026-11-23) every turn sent 54,000 characters of out-of-date browser page
views again. A view is the part of a result that starts at one of the tool's `view_markers`.
"""

from __future__ import annotations

import json

from agents.agentic_llm_agent import _OUTDATED_VIEW, _drop_outdated_views
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class _Viewer(Tool):
    name = "browser"
    description = "shows pages"
    view_markers = ("### Page now", "### Accessibility Tree")

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True)


class _Plain(Tool):
    name = "bash"
    description = "runs commands"

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True)


TOOLS = {"browser": _Viewer(), "bash": _Plain()}


def _exchange(call_id: str, tool: str, result: dict) -> list[dict]:
    call = {"id": call_id, "type": "function", "function": {"name": tool, "arguments": "{}"}}
    return [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
    ]


def _formatted(messages: list[dict], call_id: str) -> str:
    message = next(m for m in messages if m.get("tool_call_id") == call_id)
    return json.loads(message["content"])["formatted"]


def test_only_the_newest_view_stays() -> None:
    messages = [
        {"role": "user", "content": "task"},
        *_exchange("1", "browser", {"success": True, "formatted": '{"x": 1}\n\n### Page now: A - a\nuid=n1 "A"'}),
        *_exchange("2", "bash", {"success": True, "formatted": "### Page now is only text in a bash result"}),
        *_exchange(
            "3", "browser", {"success": False, "error": "e", "formatted": "### Steps\n1. ok\n\n### Page now: B"}
        ),
        *_exchange("4", "browser", {"success": True, "formatted": "URL: c\n### Accessibility Tree:\nuid=n9"}),
    ]

    _drop_outdated_views(messages, TOOLS)

    assert _formatted(messages, "1") == f'{{"x": 1}}\n\n{_OUTDATED_VIEW}'
    assert _formatted(messages, "3") == f"### Steps\n1. ok\n\n{_OUTDATED_VIEW}"
    assert _formatted(messages, "4") == "URL: c\n### Accessibility Tree:\nuid=n9"  # the newest, whole
    assert _formatted(messages, "2") == "### Page now is only text in a bash result"  # another tool's text
    assert json.loads(messages[6]["content"])["error"] == "e"  # the rest of a result is kept


def test_a_result_that_is_only_a_view_keeps_a_note() -> None:
    messages = [
        *_exchange("1", "browser", {"success": True, "formatted": "### Page now: A"}),
        *_exchange("2", "browser", {"success": True, "formatted": "### Page now: B"}),
    ]

    _drop_outdated_views(messages, TOOLS)

    assert _formatted(messages, "1") == _OUTDATED_VIEW


def test_results_without_a_view_are_never_rewritten() -> None:
    """A rewritten message changes the cached conversation from there on, so only a view is ever cut."""
    messages = [
        *_exchange("1", "browser", {"success": True, "formatted": "### Page now: A"}),
        *_exchange("2", "browser", {"success": True, "formatted": '{"action": "goto"}'}),
        *_exchange("3", "browser", {"success": True, "formatted": "### Page now: B"}),
    ]
    _drop_outdated_views(messages, TOOLS)
    before = [dict(message) for message in messages]

    messages += _exchange("4", "browser", {"success": True, "formatted": '{"action": "query"}'})
    _drop_outdated_views(messages, TOOLS)

    assert messages[: len(before)] == before  # nothing changed: the newest view is still the one in result 3
