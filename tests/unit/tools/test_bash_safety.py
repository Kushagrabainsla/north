"""Tests for BashTool safety: the approval layer, and the OS sandbox that replaced the command-string fast path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import approving_store, bind_approvals, deciding, rejecting_store
from tools.models import ToolInput
from tools.specialized import _os_sandbox
from tools.specialized.bash import BashTool

# ---------------------------------------------------------------------------
# BashTool through the approval layer - integration of safety layers
# ---------------------------------------------------------------------------


class TestBashToolApprovalBypass:
    """What bash does before it would surface a card.

    The tool no longer decides any of this - it reports facts about the command
    and `ApprovalPolicy` rules on them. These assert the outcome the user sees.
    """

    @staticmethod
    def _tool(*, mode=None, decider=None, store=None) -> tuple[BashTool, MagicMock]:
        store = store or MagicMock()
        return bind_approvals(BashTool(), mode, store=store, decider=decider), store

    @staticmethod
    async def _allowed(tool: BashTool, command: str) -> bool:
        request = await tool.describe(ToolInput(params={"command": command}))
        return (await tool.approvals.decide(request, task_id="task-1")).allowed

    @pytest.mark.asyncio
    async def test_without_the_os_sandbox_every_command_asks(self) -> None:
        """No string match decides a command is harmless any more: `git status` asks too."""
        tool, _ = self._tool(store=rejecting_store())

        assert not await self._allowed(tool, "git status")

    @pytest.mark.asyncio
    async def test_safe_never_lets_a_model_approve_a_command(self) -> None:
        """Safe is deterministic: off the safe list and never answered before, it asks."""
        from config.approval_mode import ApprovalMode

        tool, _ = self._tool(mode=ApprovalMode.SAFE, store=rejecting_store())

        assert not await self._allowed(tool, "npm run deploy")

    @pytest.mark.asyncio
    async def test_a_learned_rule_is_not_consulted_in_interactive(self) -> None:
        """The bug this replaced: the model tier ran in the default mode."""
        decider = deciding("approved")
        tool, _ = self._tool(decider=decider, store=rejecting_store())

        assert not await self._allowed(tool, "npm run deploy")
        assert not decider.asked

    @pytest.mark.asyncio
    async def test_an_auto_approved_command_is_still_recorded(self) -> None:
        """Something north did unasked has to be visible afterwards."""
        from config.approval_mode import ApprovalMode

        tool, store = self._tool(mode=ApprovalMode.SAFE)

        assert await self._allowed(tool, "pytest -q")
        store.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_mode_still_gates_a_command_off_the_allowlist(self) -> None:
        from config.approval_mode import ApprovalMode

        tool, _ = self._tool(mode=ApprovalMode.SAFE, store=rejecting_store())

        assert not await self._allowed(tool, "rm -rf /tmp/x")


class TestBashToolDestructiveBlock:
    """Verifies that obviously destructive commands are blocked before approval."""

    @pytest.mark.asyncio
    async def test_rm_rf_root_blocked(self) -> None:
        tool = bind_approvals(BashTool(), store=MagicMock())
        result = await tool.execute(ToolInput(params={"command": "rm -rf /"}))
        assert result.success is False
        assert "recognised as catastrophic" in result.error

    @pytest.mark.asyncio
    async def test_dd_blocked(self) -> None:
        tool = bind_approvals(BashTool(), store=MagicMock())
        result = await tool.execute(ToolInput(params={"command": "dd if=/dev/zero of=/dev/sda"}))
        assert result.success is False
        assert "recognised as catastrophic" in result.error


# ---------------------------------------------------------------------------
# allow_dangerous: autonomous mode lifts the hard-refusal of destructive patterns
# ---------------------------------------------------------------------------


class TestBashAllowDangerous:
    @pytest.mark.asyncio
    async def test_destructive_pattern_blocked_by_default(self) -> None:
        tool = bind_approvals(BashTool(), store=MagicMock())
        out = await tool.execute(ToolInput(params={"command": "rm -rf / --no-preserve-root"}))
        assert out.success is False
        assert "recognised as catastrophic" in (out.error or "")

    @pytest.mark.asyncio
    async def test_destructive_pattern_allowed_when_allow_dangerous(self, monkeypatch) -> None:
        # With allow_dangerous the pre-approval hard block is lifted; the command
        # proceeds (we stub execution so nothing actually runs).
        import asyncio

        async def fake_exec_shell(cmd, **kwargs):
            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

                def kill(self):
                    pass

            return _P()

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_exec_shell)
        store = MagicMock()
        resolved = MagicMock(status="approved", chosen_option="Run")
        store.wait_for_decision = AsyncMock(return_value=resolved)
        from config.approval_mode import ApprovalMode

        tool = bind_approvals(BashTool(), ApprovalMode.YOLO, store=store)
        out = await tool.execute(ToolInput(params={"command": "rm -rf / --no-preserve-root"}))
        assert out.success is True  # not pre-blocked; reached execution

    @pytest.mark.asyncio
    async def test_timeout_kills_process_group(self, monkeypatch) -> None:
        killed = False
        killpg_called = False

        class _P:
            pid = 12345
            returncode = -9
            called = 0

            async def communicate(self):
                self.called += 1
                if self.called == 1:
                    await asyncio.sleep(10)
                return b"", b""

            def kill(self):
                nonlocal killed
                killed = True

        async def fake_exec_shell(cmd, **kwargs):
            return _P()

        def fake_killpg(pgid, sig):
            nonlocal killpg_called
            killpg_called = True

        import asyncio
        import os

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_exec_shell)
        monkeypatch.setattr(os, "killpg", fake_killpg)
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

        store = MagicMock()
        resolved = MagicMock(status="approved", chosen_option="Run")
        store.wait_for_decision = AsyncMock(return_value=resolved)

        from config.approval_mode import ApprovalMode

        tool = bind_approvals(BashTool(), ApprovalMode.YOLO, store=store)
        out = await tool.execute(ToolInput(params={"command": "sleep 100", "timeout": 1}))

        assert out.success is False
        assert "timed out" in (out.error or "")
        assert killpg_called is True


class TestBashApprovalOutcomes:
    """A command the user declined is a refusal, not a broken tool."""

    @staticmethod
    def _tool(store):
        return bind_approvals(BashTool(), store=store)

    @pytest.mark.asyncio
    async def test_a_declined_command_says_the_user_cancelled_it(self) -> None:
        store = MagicMock()
        resolved = MagicMock()
        resolved.status = "rejected"
        resolved.chosen_option = "Cancel"
        store.wait_for_decision = AsyncMock(return_value=resolved)

        result = await self._tool(store).execute(ToolInput(params={"command": "rm -rf build"}))

        assert result.success is False
        assert result.failure_kind == "refused"
        assert result.error == "Command cancelled by user."


@pytest.mark.skipif(_os_sandbox.current() is None, reason="needs macOS Seatbelt")
class TestBashOsSandbox:
    """The kernel, not a reading of the command, decides what runs without a card."""

    @staticmethod
    def _tool(store=None) -> BashTool:
        return bind_approvals(BashTool(os_sandbox=True), store=store or rejecting_store())

    @pytest.mark.asyncio
    async def test_a_command_that_only_reads_runs_without_a_card(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        tool = self._tool()

        out = await tool.execute(ToolInput(params={"command": "cat a.txt", "workspace": str(tmp_path)}))

        assert out.success
        assert out.data["stdout"] == "hello"

    @pytest.mark.asyncio
    async def test_a_write_is_refused_by_the_kernel_and_goes_to_approval(self, tmp_path) -> None:
        tool = self._tool()  # a rejecting store: asked, and the answer is no

        out = await tool.execute(ToolInput(params={"command": "touch made.txt", "workspace": str(tmp_path)}))

        assert out.failure_kind == "refused"
        assert not (tmp_path / "made.txt").exists()

    @pytest.mark.asyncio
    async def test_a_write_hidden_in_a_safe_looking_command_cannot_skip_the_card(self, tmp_path) -> None:
        """What the string fast path let through: `cat` with a redirect, a `$(...)`, `git` with a pager."""
        tool = self._tool()

        for command in ("cat /etc/hosts > made.txt", "echo $(touch made.txt)", "ls; touch made.txt"):
            out = await tool.execute(ToolInput(params={"command": command, "workspace": str(tmp_path)}))
            assert out.failure_kind == "refused", command
        assert not (tmp_path / "made.txt").exists()

    NEEDS_A_SCRATCH_FILE = "python3 -c \"import tempfile; tempfile.NamedTemporaryFile().write(b'x'); print('done')\""

    @pytest.mark.asyncio
    async def test_a_command_that_failed_in_the_read_only_probe_is_not_taken_as_read_only(self, tmp_path) -> None:
        """Its failure (here: no scratch file) says nothing about whether it would change anything."""
        tool = self._tool()  # a rejecting store: asked, and the answer is no

        out = await tool.execute(ToolInput(params={"command": self.NEEDS_A_SCRATCH_FILE, "workspace": str(tmp_path)}))

        assert out.failure_kind == "refused", (
            "it went to approval instead of passing its failed probe off as the result"
        )

    @pytest.mark.asyncio
    async def test_once_approved_that_command_runs_where_it_may_write_and_succeeds(self, tmp_path) -> None:
        tool = bind_approvals(BashTool(os_sandbox=True), store=approving_store())

        out = await tool.execute(ToolInput(params={"command": self.NEEDS_A_SCRATCH_FILE, "workspace": str(tmp_path)}))

        assert out.success and out.data["stdout"].strip() == "done"

    @pytest.mark.asyncio
    async def test_a_read_only_command_that_succeeds_still_runs_once_without_asking(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        tool = self._tool()

        out = await tool.execute(ToolInput(params={"command": "cat a.txt", "workspace": str(tmp_path)}))

        assert out.success and out.data["stdout"] == "hello"

    @pytest.mark.asyncio
    async def test_a_credential_directory_is_unreadable_even_to_an_approved_command(self, tmp_path) -> None:
        tool = bind_approvals(BashTool(os_sandbox=True), store=approving_store())

        out = await tool.execute(ToolInput(params={"command": "ls ~/.ssh", "workspace": str(tmp_path)}))

        assert "not permitted" in (out.error or "")

    @pytest.mark.asyncio
    async def test_an_approved_command_writes_only_inside_its_workspace(self, tmp_path) -> None:
        tool = bind_approvals(BashTool(os_sandbox=True), store=approving_store())

        inside = await tool.execute(ToolInput(params={"command": "touch made.txt", "workspace": str(tmp_path)}))
        outside = await tool.execute(
            ToolInput(params={"command": "touch /Users/Shared/north-sandbox-probe", "workspace": str(tmp_path)})
        )

        assert inside.success and (tmp_path / "made.txt").exists()
        assert not outside.success

    @pytest.mark.asyncio
    async def test_a_read_that_needs_the_network_asks(self, tmp_path) -> None:
        tool = self._tool()

        out = await tool.execute(
            ToolInput(params={"command": "curl -sS -m 3 https://example.com", "workspace": str(tmp_path)})
        )

        assert out.failure_kind == "refused"

    def test_docker_replaces_the_os_sandbox(self) -> None:
        from tools.specialized._sandbox import SandboxConfig

        assert BashTool(sandbox=SandboxConfig(enabled=True), os_sandbox=True)._os_sandbox is None


def test_a_platform_with_no_sandbox_asks_for_every_command(monkeypatch) -> None:
    monkeypatch.setattr(_os_sandbox, "_IMPLEMENTATIONS", ())

    assert BashTool(os_sandbox=True)._os_sandbox is None
