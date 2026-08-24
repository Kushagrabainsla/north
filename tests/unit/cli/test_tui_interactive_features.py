"""Tests for interactive TUI features: reasoning drawer, tool inspector, plan cockpit, and steering."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli.formatting import _format_plan_table
from cli.tui import (
    NorthApp,
    PlanCockpitModal,
    ToolInspectorModal,
)

_DEAD = "http://127.0.0.1:1"
_HEADERS = {"X-North-Secret": "x"}


class _FakeResp:
    def __init__(self, js, status_code=200):
        self._js = js
        self.status_code = status_code

    def json(self):
        return self._js


class _CapturingClient:
    def __init__(self, sink: list[dict]):
        self.sink = sink

    async def post(self, url, **kw):
        self.sink.append({"url": url, "json": kw.get("json")})
        return _FakeResp({"status": "ok", "task_id": "t1"})

    async def get(self, url, **kw):
        return _FakeResp([])


def _install_capturing_http(app: NorthApp) -> list[dict]:
    sink: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_http():
        yield _CapturingClient(sink)

    app._http = fake_http
    return sink


@pytest.mark.asyncio
async def test_reasoning_drawer_toggle_and_buffer_accumulation():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not app._reasoning_visible
        
        # Toggle thoughts on
        app.action_toggle_reasoning()
        assert app._reasoning_visible
        assert app.query_one("#reasoning-wrap").styles.display == "block"

        # Stream reasoning events
        tid = "t_reason"
        app._user_task_ids.add(tid)
        await app._handle_event("reasoning", {"task_id": tid, "text": "Analyzing the database schema..."})
        await app._handle_event("reasoning", {"task_id": tid, "text": " Let's check migrations."})
        await pilot.pause()

        assert app._reasoning_buffer[tid] == "Analyzing the database schema... Let's check migrations."
        header_text = str(app.query_one("#reasoning-header").render())
        assert "Thinking" in header_text

        # Start token stream -> preserves thoughts in _recent_thoughts
        await app._handle_event("token", {"task_id": tid, "text": "Here is the plan."})
        await pilot.pause()

        assert len(app._recent_thoughts) == 1
        assert "Analyzing the database schema" in app._recent_thoughts[0]["thoughts"]


@pytest.mark.asyncio
async def test_tool_history_recording_and_inspector_modal():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "t_tool"
        app._user_task_ids.add(tid)
        
        # Tool call and result
        await app._handle_event("tool_called", {
            "task_id": tid,
            "tool": "patch_file",
            "params": {"path": "auth.py", "old_string": "x = 1", "new_string": "x = 2"},
        })
        await pilot.pause()
        assert len(app._tool_history) == 1
        assert app._tool_history[0]["tool"] == "patch_file"

        await app._handle_event("tool_result", {
            "task_id": tid,
            "tool": "patch_file",
            "success": True,
            "formatted": "Patched 1 block successfully.",
        })
        await pilot.pause()
        assert app._tool_history[0]["success"] is True
        assert "Patched 1 block" in app._tool_history[0]["formatted"]

        # Open ToolInspectorModal
        modal = ToolInspectorModal(list(app._tool_history))
        app.push_screen(modal)
        await pilot.pause()

        assert isinstance(app.screen, ToolInspectorModal)
        # Dismiss modal
        app.pop_screen()
        await pilot.pause()
        assert not isinstance(app.screen, ToolInspectorModal)


@pytest.mark.asyncio
async def test_plan_cockpit_modal_and_dod_evaluation():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "t_plan"
        app._user_task_ids.add(tid)

        await app._handle_event("plan_seeded", {
            "task_id": tid,
            "tasks": 2,
            "steps": [
                {"step_id": 1, "agent": "coder", "task": "Write schema", "status": "done"},
                {"step_id": 2, "agent": "verifier", "task": "Run tests", "status": "in_progress"},
            ],
        })
        await app._handle_event("dod_evaluated", {
            "task_id": tid,
            "passed": True,
            "reasons": ["all test assertions passed", "clean git diff"],
        })
        await pilot.pause()

        assert len(app._plan_steps) == 2
        assert len(app._dod_results) == 1

        # Test plan table formatter
        table = _format_plan_table(app._plan_steps, app._dod_results)
        assert table.row_count >= 3

        # Open PlanCockpitModal
        modal = PlanCockpitModal(list(app._plan_steps), list(app._dod_results))
        app.push_screen(modal)
        await pilot.pause()

        assert isinstance(app.screen, PlanCockpitModal)
        app.pop_screen()
        await pilot.pause()
        assert not isinstance(app.screen, PlanCockpitModal)


@pytest.mark.asyncio
async def test_steering_submission_and_slash_commands():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    sink = _install_capturing_http(app)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # Submit /steer command
        prompt = app.query_one("#prompt")
        prompt.value = "/steer use pytest-asyncio strictly"
        await app.on_input_submitted(SimpleNamespace(value=prompt.value, input=prompt))
        await pilot.pause()

        assert any(
            "steer" in call["url"] and call["json"]["instruction"] == "use pytest-asyncio strictly" for call in sink
        )

        # Test /thoughts slash command
        prompt.value = "/thoughts"
        await app.on_input_submitted(SimpleNamespace(value=prompt.value, input=prompt))
        await pilot.pause()
        assert app._reasoning_visible is True


@pytest.mark.asyncio
async def test_orchestrator_emit_steer_and_api_endpoint():
    from approval.store import ApprovalStore
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.stream import EventStreamManager

    stream_mgr = EventStreamManager()
    approval_store = ApprovalStore()
    ledger_writer = MagicMock()
    ledger_writer.write = AsyncMock()

    orch = Orchestrator(
        ledger=ledger_writer,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream_mgr,
        approval_store=approval_store,
    )

    await orch.emit_steer("task_123", "focus on edge cases")

    assert ledger_writer.write.call_count == 1
    entry = ledger_writer.write.call_args[0][0]
    assert entry.action == "task_steered"
    assert entry.output == "focus on edge cases"
