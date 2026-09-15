from __future__ import annotations

from cli.tui_text import estimated_tokens, requested_context_document, slash_argument


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
