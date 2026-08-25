"""Tests for collapsible turn activity traces and Ctrl+O toggles in North TUI."""

from __future__ import annotations

import pytest

from cli.formatting import _format_turn_details, _format_turn_summary
from cli.tui import NorthApp

_DEAD = "http://127.0.0.1:1"
_HEADERS = {"X-North-Secret": "x"}


def test_format_turn_summary_with_tools_and_thoughts() -> None:
    turn = {
        "prompt": "inspect sqlite db",
        "domain": "general",
        "agents": ["general"],
        "thought_duration": 1.85,
        "thought_tokens": 45,
        "tools": [
            {"tool": "read_file", "result": "ok", "success": True},
            {"tool": "read_file", "result": "ok", "success": True},
            {"tool": "search_files", "result": "3 matches", "success": True},
        ],
        "verifications": [{"command": "pytest", "passed": True}],
    }
    summary = _format_turn_summary(turn)
    assert "3 tools (2 read_file, search_files)" in summary
    assert "1.9s thoughts" in summary
    assert "1/1 checks passed" in summary
    assert "Ctrl+O to expand" in summary


def test_format_turn_summary_direct_answer() -> None:
    turn = {"prompt": "hello", "domain": "general", "agents": []}
    summary = _format_turn_summary(turn)
    assert "general agent" in summary or "direct answer" in summary


def test_format_turn_details_breakdown() -> None:
    turn = {
        "prompt": "fix bug in auth",
        "domain": "coding",
        "is_consequential": True,
        "agents": ["coder", "tester"],
        "model": "stealth/ox-alpha",
        "thought_duration": 2.1,
        "thought_tokens": 80,
        "tools": [
            {"tool": "patch_file", "params_str": "path='auth.py'", "result": "applied 1 hunk", "duration": 0.05, "success": True}
        ],
        "verifications": [{"command": "pytest tests/unit/auth/", "passed": True}],
    }
    details = _format_turn_details(turn)
    text = "\n".join(details)
    assert "Execution Details" in text
    assert "classified:" in text and "coding" in text and "(complex)" in text
    assert "plan ready:" in text and "coder, tester" in text
    assert "patch_file" in text and "path='auth.py'" in text
    assert "verify(pytest tests/unit/auth/)" in text and "PASS" in text
    assert "Ctrl+O to collapse" in text


@pytest.mark.asyncio
async def test_turn_activity_lifecycle_and_details_toggle():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not app._details_expanded

        # Submit task & start tracking
        tid = "task-turn-1"
        app._user_task_ids.add(tid)
        app._pending_user_messages[tid] = "check files"
        app._current_turn_activity[tid] = {
            "task_id": tid,
            "prompt": "check files",
            "domain": "",
            "is_consequential": False,
            "agents": [],
            "model": "",
            "thought_duration": 0.0,
            "thought_tokens": 0,
            "tools": [],
            "verifications": [],
            "output": "",
            "status": "running",
            "error": "",
        }

        # Stream lifecycle events
        await app._handle_event("classified", {"task_id": tid, "domain": "general", "is_consequential": False})
        await app._handle_event("routed", {"task_id": tid, "agents": ["general"]})
        await app._handle_event("model", {"task_id": tid, "model": "stealth/ox-alpha"})
        await app._handle_event("agent_started", {"task_id": tid, "agent": "general"})
        await app._handle_event("tool_called", {"task_id": tid, "tool": "search_files", "params": {"pattern": "def"}})
        await app._handle_event("tool_result", {"task_id": tid, "tool": "search_files", "success": True, "formatted": "3 results"})
        await app._handle_event("token", {"task_id": tid, "text": "All files checked."})
        await app._handle_event("task_completed", {"task_id": tid, "cost_usd": "0.001"})
        await pilot.pause()

        # Turn finalized in history
        assert len(app._turns) == 1
        turn = app._turns[0]
        assert turn["prompt"] == "check files"
        assert turn["domain"] == "general"
        assert len(turn["tools"]) == 1
        assert turn["tools"][0]["tool"] == "search_files"
        assert turn["output"] == "All files checked."
        assert turn["status"] == "completed"

        # Toggle details with Ctrl+O
        app.action_toggle_activity_details()
        assert app._details_expanded
        hint = str(app.query_one("#hint").render())
        assert "ctrl+o collapse" in hint

        # Toggle back
        app.action_toggle_activity_details()
        assert not app._details_expanded
        hint2 = str(app.query_one("#hint").render())
        assert "ctrl+o details" in hint2
