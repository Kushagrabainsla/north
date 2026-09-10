from __future__ import annotations

from cli.tui_text import describe_turn, estimated_tokens, requested_context_document, slash_argument


def test_describe_turn_includes_only_completed_tool_actions() -> None:
    turn = {
        "user": "check the repo",
        "north": "done",
        "tools": [
            {"tool": "read_file", "params": "a.py", "result": "ok"},
            {"tool": "list_dir", "result": "two files"},
            {"tool": "pending_tool", "params": "x"},
        ],
    }

    rendered = describe_turn(turn)

    assert rendered.startswith("User: check the repo")
    assert "read_file(a.py) → ok" in rendered
    assert "list_dir → two files" in rendered
    assert "pending_tool" not in rendered
    assert rendered.endswith("north: done")


def test_describe_turn_without_tools_has_no_action_block() -> None:
    assert describe_turn({"user": "hi", "north": "hello"}) == "User: hi\nnorth: hello"


def test_estimated_tokens_never_reports_zero() -> None:
    assert estimated_tokens("") == 1
    assert estimated_tokens("abcdefgh") == 2


def test_slash_argument_and_context_document_parsing() -> None:
    assert slash_argument("/context") is None
    assert slash_argument("/context soul") == "soul"

    assert requested_context_document("/context soul.md") == "soul"
    assert requested_context_document("/context show north_stars") == "north_stars"
    assert requested_context_document("/context show") == ""
    assert requested_context_document("/context edit soul") == ""
