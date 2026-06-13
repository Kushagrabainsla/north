"""Tests for in-code approval gates on GitTool/GhTool (review findings R2#10, R2#11, R4#25)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.models import ToolInput, ToolOutput
from tools.specialized import gh_tool as gh_module
from tools.specialized import git_tool as git_module
from tools.specialized.gh_tool import GhTool
from tools.specialized.git_tool import GitTool


@pytest.fixture
def fake_run_capture(monkeypatch):
    """Stub the subprocess runner so no real git/gh ever executes."""
    calls: list[list[str]] = []

    def fake(cmd, cwd, *, timeout, max_output=20_000):
        calls.append(cmd)
        return ToolOutput(success=True, data={"command": " ".join(cmd), "stdout": "", "stderr": "", "returncode": 0})

    monkeypatch.setattr(git_module, "run_capture", fake)
    monkeypatch.setattr(gh_module, "run_capture", fake)
    monkeypatch.setattr(git_module.shutil, "which", lambda _: "/usr/bin/stub")
    monkeypatch.setattr(gh_module.shutil, "which", lambda _: "/usr/bin/stub")
    return calls


def _approving_store() -> MagicMock:
    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Approve"
    store.wait_for_decision = AsyncMock(return_value=resolved)
    return store


def _rejecting_store() -> MagicMock:
    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Reject"
    store.wait_for_decision = AsyncMock(return_value=resolved)
    return store


# ── GitTool ──────────────────────────────────────────────────────────────────


class TestGitGate:
    async def test_readonly_actions_run_without_approval(self, fake_run_capture) -> None:
        result = await GitTool().run(ToolInput(params={"action": "status"}))
        assert result.success is True
        assert fake_run_capture, "git status should have executed"

    async def test_branch_listing_runs_without_approval(self, fake_run_capture) -> None:
        result = await GitTool().run(ToolInput(params={"action": "branch", "args": "-a"}))
        assert result.success is True

    @pytest.mark.parametrize("action,args", [("commit", "msg"), ("push", ""), ("merge", "x"), ("add", ".")])
    async def test_mutating_actions_fail_closed_without_gate(self, fake_run_capture, action, args) -> None:
        result = await GitTool().run(ToolInput(params={"action": action, "args": args}))
        assert result.success is False
        assert "fail closed" in result.error
        assert not fake_run_capture, "no subprocess may run without an approval gate"

    async def test_branch_create_fails_closed_without_gate(self, fake_run_capture) -> None:
        result = await GitTool().run(ToolInput(params={"action": "branch", "args": "-D main"}))
        assert result.success is False
        assert not fake_run_capture

    async def test_mutating_action_runs_when_user_approves(self, fake_run_capture) -> None:
        tool = GitTool(approval_store=_approving_store())
        result = await tool.run(ToolInput(params={"action": "commit", "args": "fix: things"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["git", "commit", "-m"]

    async def test_mutating_action_refused_when_user_rejects(self, fake_run_capture) -> None:
        tool = GitTool(approval_store=_rejecting_store())
        result = await tool.run(ToolInput(params={"action": "push", "args": "origin main"}))
        assert result.success is False
        assert not fake_run_capture

    @pytest.mark.parametrize(
        "args",
        [
            "--force",
            "-f",
            "--force-with-lease",
            "--force-with-lease=refs/heads/main",
            "origin main --force",  # reordered flags must not bypass the block
            "origin -f main",
        ],
    )
    async def test_force_push_always_blocked_even_with_approval(self, fake_run_capture, args) -> None:
        tool = GitTool(approval_store=_approving_store())
        result = await tool.run(ToolInput(params={"action": "push", "args": args}))
        assert result.success is False
        assert "blocked" in result.error.lower()
        assert not fake_run_capture

    async def test_reset_and_clean_are_not_offered(self, fake_run_capture) -> None:
        for action in ("reset", "clean"):
            result = await GitTool().run(ToolInput(params={"action": action, "args": "--hard"}))
            assert result.success is False
            assert "Unknown git action" in result.error


# ── GhTool ───────────────────────────────────────────────────────────────────


class TestGhGate:
    async def test_readonly_action_runs_without_approval(self, fake_run_capture) -> None:
        result = await GhTool().run(ToolInput(params={"action": "pr_view", "args": "123"}))
        assert result.success is True

    @pytest.mark.parametrize(
        "action", ["pr_create", "pr_comment", "pr_merge", "pr_review", "issue_create", "issue_comment"]
    )
    async def test_mutating_actions_fail_closed_without_gate(self, fake_run_capture, action) -> None:
        result = await GhTool().run(ToolInput(params={"action": action, "args": "123"}))
        assert result.success is False
        assert "fail closed" in result.error
        assert not fake_run_capture

    async def test_pr_merge_runs_only_after_approval(self, fake_run_capture) -> None:
        tool = GhTool(approval_store=_approving_store())
        result = await tool.run(ToolInput(params={"action": "pr_merge", "args": "123"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["gh", "pr", "merge"]

    async def test_pr_merge_refused_on_reject(self, fake_run_capture) -> None:
        tool = GhTool(approval_store=_rejecting_store())
        result = await tool.run(ToolInput(params={"action": "pr_merge", "args": "123"}))
        assert result.success is False
        assert not fake_run_capture
