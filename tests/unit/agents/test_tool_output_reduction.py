"""High-signal evidence survives deterministic tool-output reduction."""

from __future__ import annotations

import json

from agents.agentic_llm_agent import _cap_tool_result
from agents.constants import MAX_TOOL_RESULT_CHARS
from agents.tool_output_reduction import salient_excerpt
from tools.output_spill import spill_store


def test_salient_excerpt_keeps_middle_error_and_source_location() -> None:
    text = "header\n" + "ordinary output\n" * 500 + "src/auth.py:417: ERROR invalid token\n" + "tail output\n" * 500

    reduced = salient_excerpt(text, 500)

    assert len(reduced) <= 500
    assert reduced.startswith("header")
    assert "src/auth.py:417: ERROR invalid token" in reduced
    assert reduced.endswith("tail output\n")


def test_salient_excerpt_keeps_middle_symbol_declaration() -> None:
    text = "start\n" + "noise\n" * 300 + "class AuthenticationCoordinator:\n" + "more\n" * 300

    reduced = salient_excerpt(text, 300)

    assert "class AuthenticationCoordinator:" in reduced


def test_cap_tool_result_keeps_signal_while_remaining_valid_bounded_json() -> None:
    spill_store().clear()
    text = "a" * 30_000 + "\nsrc/router.py:88: ERROR route missing\n" + "z" * 30_000

    capped = _cap_tool_result({"success": True, "data": {"formatted": text}}, "read_file")
    result = json.loads(capped)

    assert len(capped) <= MAX_TOOL_RESULT_CHARS
    assert "src/router.py:88: ERROR route missing" in result["data"]["formatted"]
    assert result["_handle"]
