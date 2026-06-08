"""Tests for BashTool safety layers: CommandSafetyInspector and JudgementFilter bypass."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.models import ToolInput
from tools.specialized.bash import BashTool, CommandSafetyInspector

# ---------------------------------------------------------------------------
# CommandSafetyInspector
# ---------------------------------------------------------------------------


class TestCommandSafetyInspector:
    """Verifies the local regex bypass for read-only commands."""

    def setup_method(self) -> None:
        self.inspector = CommandSafetyInspector()

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git diff HEAD~2",
            "git log --oneline -5",
            "git show abc123",
            "git branch -a",
            "cat README.md",
            "grep -r 'TODO' src/",
            "find . -name '*.py'",
            "ls -la /tmp",
            "pwd",
            "whoami",
        ],
    )
    def test_read_only_commands_are_safe(self, command: str) -> None:
        assert self.inspector.is_instantly_safe(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "git push origin main",
            "git commit -m 'wip'",
            "pip install requests",
            "npm install",
            "python manage.py migrate",
            "docker rm -f $(docker ps -aq)",
            "echo 'hello' > file.txt",
            "curl https://example.com",
        ],
    )
    def test_mutating_commands_are_not_safe(self, command: str) -> None:
        assert self.inspector.is_instantly_safe(command) is False

    def test_case_insensitive(self) -> None:
        assert self.inspector.is_instantly_safe("GIT STATUS") is True
        assert self.inspector.is_instantly_safe("Cat /etc/hosts") is True

    def test_leading_whitespace_is_trimmed(self) -> None:
        assert self.inspector.is_instantly_safe("   git status") is True


# ---------------------------------------------------------------------------
# BashTool._request_approval — integration of safety layers
# ---------------------------------------------------------------------------


class TestBashToolApprovalBypass:
    """Verifies that _request_approval short-circuits correctly."""

    def _make_tool(self, *, judgement_filter: MagicMock | None = None) -> BashTool:
        return BashTool(
            approval_store=MagicMock(),
            stream_manager=None,
            approval_timeout_seconds=5.0,
            judgement_filter=judgement_filter,
        )

    @pytest.mark.asyncio
    async def test_instantly_safe_command_skips_all_gates(self) -> None:
        tool = self._make_tool()
        approved = await tool._request_approval("task-1", "git status")
        assert approved is True
        # approval_store.add should never have been called
        tool._approval_store.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_judgement_filter_auto_approves(self) -> None:
        jf = MagicMock()
        jf.check = AsyncMock(return_value=("approved", "learned rule"))
        tool = self._make_tool(judgement_filter=jf)

        approved = await tool._request_approval("task-1", "npm test")
        assert approved is True
        jf.check.assert_awaited_once()
        tool._approval_store.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_judgement_filter_auto_rejects(self) -> None:
        jf = MagicMock()
        jf.check = AsyncMock(return_value=("rejected", "user rule"))
        tool = self._make_tool(judgement_filter=jf)

        approved = await tool._request_approval("task-1", "rm -rf node_modules")
        assert approved is False

    @pytest.mark.asyncio
    async def test_judgement_filter_undecided_falls_through_to_manual(self) -> None:
        jf = MagicMock()
        jf.check = AsyncMock(return_value=("undecided", None))
        tool = self._make_tool(judgement_filter=jf)

        # Simulate user approving via the approval store
        resolved_card = MagicMock()
        resolved_card.chosen_option = "Run"
        tool._approval_store.wait_for_decision = AsyncMock(return_value=resolved_card)

        approved = await tool._request_approval("task-1", "python setup.py install")
        assert approved is True
        tool._approval_store.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_judgement_filter_exception_falls_through(self) -> None:
        jf = MagicMock()
        jf.check = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        tool = self._make_tool(judgement_filter=jf)

        resolved_card = MagicMock()
        resolved_card.chosen_option = "Run"
        tool._approval_store.wait_for_decision = AsyncMock(return_value=resolved_card)

        approved = await tool._request_approval("task-1", "make build")
        assert approved is True  # fell through to manual, user approved


# ---------------------------------------------------------------------------
# BashTool.run — end-to-end with obvious destructive check
# ---------------------------------------------------------------------------


class TestBashToolDestructiveBlock:
    """Verifies that obviously destructive commands are blocked before approval."""

    @pytest.mark.asyncio
    async def test_rm_rf_root_blocked(self) -> None:
        tool = BashTool(approval_store=MagicMock(), stream_manager=None)
        result = await tool.run(ToolInput(params={"command": "rm -rf /"}))
        assert result.success is False
        assert "Blocked pattern" in result.error

    @pytest.mark.asyncio
    async def test_dd_blocked(self) -> None:
        tool = BashTool(approval_store=MagicMock(), stream_manager=None)
        result = await tool.run(ToolInput(params={"command": "dd if=/dev/zero of=/dev/sda"}))
        assert result.success is False
        assert "Blocked pattern" in result.error
