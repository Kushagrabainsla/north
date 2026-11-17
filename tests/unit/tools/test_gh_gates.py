"""Tests for the in-code approval gate on GhTool (review findings R2#10, R2#11, R4#25).

The git tool these shared a file with is gone: the shell runs git (issue #78 follow-up).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import bind_approvals
from tools.models import ToolInput, ToolOutput
from tools.specialized import gh as gh_module
from tools.specialized.gh import GhTool


@pytest.fixture
def fake_run_capture(monkeypatch):
    """Stub the subprocess runner so no real gh ever executes."""
    calls: list[list[str]] = []

    def fake(cmd, cwd, *, timeout, max_output=20_000):
        calls.append(cmd)
        return ToolOutput(success=True, data={"command": " ".join(cmd), "stdout": "", "stderr": "", "returncode": 0})

    monkeypatch.setattr(gh_module, "run_capture", fake)
    monkeypatch.setattr(gh_module.shutil, "which", lambda _: "/usr/bin/stub")
    return calls


def _approving_store() -> MagicMock:
    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Approve"
    resolved.status = "approved"
    store.wait_for_decision = AsyncMock(return_value=resolved)
    return store


def _rejecting_store() -> MagicMock:
    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Reject"
    resolved.status = "rejected"
    store.wait_for_decision = AsyncMock(return_value=resolved)
    return store


class TestGhGate:
    async def test_readonly_action_runs_without_approval(self, fake_run_capture) -> None:
        result = await GhTool().execute(ToolInput(params={"action": "pr_view", "args": "123"}))
        assert result.success is True

    @pytest.mark.parametrize(
        "action", ["pr_create", "pr_comment", "pr_merge", "pr_review", "issue_create", "issue_comment"]
    )
    async def test_mutating_actions_fail_closed_without_gate(self, fake_run_capture, action) -> None:
        result = await GhTool().execute(ToolInput(params={"action": action, "args": "123"}))
        assert result.success is False
        assert "fail closed" in result.error
        assert not fake_run_capture

    async def test_pr_merge_runs_only_after_approval(self, fake_run_capture) -> None:
        tool = bind_approvals(GhTool(), store=_approving_store())
        result = await tool.execute(ToolInput(params={"action": "pr_merge", "args": "123"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["gh", "pr", "merge"]

    async def test_pr_merge_refused_on_reject(self, fake_run_capture) -> None:
        tool = bind_approvals(GhTool(), store=_rejecting_store())
        result = await tool.execute(ToolInput(params={"action": "pr_merge", "args": "123"}))
        assert result.success is False
        assert not fake_run_capture

    async def test_pr_status_runs_without_approval(self, fake_run_capture) -> None:
        result = await GhTool().execute(ToolInput(params={"action": "pr_status"}))
        assert result.success is True
        assert fake_run_capture[0][:2] == ["gh", "pr"]

    async def test_pr_checks_runs_without_approval(self, fake_run_capture) -> None:
        result = await GhTool().execute(ToolInput(params={"action": "pr_checks", "args": "123"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["gh", "pr", "checks"]

    async def test_pr_ready_fails_closed_without_gate(self, fake_run_capture) -> None:
        result = await GhTool().execute(ToolInput(params={"action": "pr_ready", "args": "123"}))
        assert result.success is False
        assert "fail closed" in result.error
        assert not fake_run_capture

    async def test_pr_ready_runs_only_after_approval(self, fake_run_capture) -> None:
        tool = bind_approvals(GhTool(), store=_approving_store())
        result = await tool.execute(ToolInput(params={"action": "pr_ready", "args": "123"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["gh", "pr", "ready"]


async def test_a_mutating_github_call_leaves_the_sandbox_and_a_read_does_not(fake_run_capture) -> None:
    """Autonomous approves these only when something you stated covers them (approval/decider.py)."""
    write = await GhTool().describe(ToolInput(params={"action": "pr_create", "args": "--title x"}))
    read = await GhTool().describe(ToolInput(params={"action": "pr_list"}))

    assert write.action.leaves_sandbox is True
    assert read is None or read.action.leaves_sandbox is False
