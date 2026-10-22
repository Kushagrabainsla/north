"""The run-history record of a tool result keeps what the model was shown."""

from __future__ import annotations

import json

from agents.agentic_llm_agent import _tool_result_event


def test_a_failed_result_keeps_its_data_beside_the_error() -> None:
    """A failed skill test logged only its error, though the model saw which prompt failed."""
    result = {
        "success": False,
        "error": "Skill selection tests failed.",
        "data": {"positive_failures": [], "negative_failures": ["Summarize a HubSpot marketing page."]},
    }

    event = _tool_result_event("create_skill", json.dumps(result), False)

    assert event["error"] == "Skill selection tests failed."
    assert event["data"]["negative_failures"] == ["Summarize a HubSpot marketing page."]
    assert "positive_failures" in event["data"]


def test_a_result_without_formatted_text_keeps_its_fields() -> None:
    """An ask_user answer was missing from the run history."""
    result = {"success": True, "answered": True, "answer": "Once at a specific date and time"}

    event = _tool_result_event("ask_user", json.dumps(result), True)

    assert event == {
        "tool": "ask_user",
        "success": True,
        "answered": True,
        "answer": "Once at a specific date and time",
    }


def test_formatted_text_stands_for_the_data() -> None:
    result = {"success": True, "data": {"rows": [1, 2, 3]}, "formatted": "3 rows"}

    event = _tool_result_event("query", json.dumps(result), True)

    assert event == {"tool": "query", "success": True, "formatted": "3 rows"}


def test_a_result_that_is_not_a_json_object_records_only_the_outcome() -> None:
    assert _tool_result_event("bash", "plain text", True) == {"tool": "bash", "success": True}
    assert _tool_result_event("bash", "[1, 2]", True) == {"tool": "bash", "success": True}
