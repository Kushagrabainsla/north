"""Tests for BashTool safety: the approval layer, and the OS sandbox that replaced the command-string fast path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coding_agents.constants import VERIFY_TIMEOUT_SECONDS
from tests.conftest import approving_store, bind_approvals, deciding, rejecting_store
from tools.models import ToolInput, ToolOutput
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
    async def test_a_safe_listed_command_that_writes_outside_its_folder_asks(self) -> None:
        """`pytest` is on the safe list; writing to a folder the task was not given is still yours to allow."""
        from config.approval_mode import ApprovalMode

        tool, _ = self._tool(mode=ApprovalMode.SAFE, store=rejecting_store())
        params = {"command": "pytest -q", "write_outside": ["/Users/Shared/elsewhere"]}
        request = await tool.describe(ToolInput(params=params, granted_workspace="/Users/Shared/task"))

        assert "/Users/Shared/elsewhere" in request.message
        assert not (await tool.approvals.decide(request, task_id="task-1")).allowed

    @pytest.mark.asyncio
    async def test_the_same_command_in_another_folder_is_another_action(self) -> None:
        """A yes to `rm -rf build` in one folder is not a yes in another (issue #76, A8)."""
        tool, _ = self._tool()

        def describe(folder):
            params = {"command": "rm -rf build", "workspace": folder}
            return tool.describe(ToolInput(params=params, granted_workspace="/Users/Shared"))

        here = await describe("/Users/Shared/a")
        there = await describe("/Users/Shared/b")

        assert here.action.describe() != there.action.describe()

    @pytest.mark.asyncio
    async def test_an_in_place_edit_inside_a_git_repository_is_refused_without_a_card(self, tmp_path) -> None:
        """`sed -i` on a repository's file is coding_agent's work (issue #76, M12); running its tests is not refused."""
        from config.approval_mode import ApprovalMode

        repo = tmp_path / "shop"
        (repo / ".git").mkdir(parents=True)
        tool, store = self._tool(mode=ApprovalMode.YOLO)

        def call(command):
            return ToolInput(params={"command": command, "workspace": str(repo)}, granted_workspace=str(repo))

        edit = await tool.approvals.decide(await tool.describe(call("sed -i 's|a|b|' pricing.py")), task_id="t1")
        tests = await tool.approvals.decide(await tool.describe(call("pytest -q")), task_id="t1")

        assert not edit.allowed and "coding_agent" in edit.reason
        assert tests.allowed

    @pytest.mark.asyncio
    async def test_a_credential_folder_cannot_be_asked_for(self) -> None:
        tool, store = self._tool(store=approving_store())
        params = {"command": "touch ~/.ssh/key", "write_outside": ["~/.ssh"]}

        out = await tool.execute(ToolInput(params=params, granted_workspace="/Users/Shared/task"))

        assert out.failure_kind == "refused" and "credentials" in out.error
        store.add.assert_not_called()

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
                stdout = stderr = None

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
        killpg_called = False

        class _P:
            """Still running until its group is signalled."""

            pid = 12345
            returncode = None

            async def communicate(self):
                await asyncio.sleep(10)
                return b"", b""

            async def wait(self):
                return self.returncode

        proc = _P()

        async def fake_exec_shell(cmd, **kwargs):
            return proc

        def fake_killpg(pgid, sig):
            nonlocal killpg_called
            killpg_called = True
            proc.returncode = -sig

        import asyncio
        import os

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_exec_shell)
        monkeypatch.setattr(os, "killpg", fake_killpg)

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
        """Its failure says nothing about whether it would change anything."""
        tool = self._tool()  # a rejecting store: asked, and the answer is no

        out = await tool.execute(ToolInput(params={"command": "exit 3", "workspace": str(tmp_path)}))

        assert out.failure_kind == "refused", (
            "it went to approval instead of passing its failed probe off as the result"
        )

    @pytest.mark.asyncio
    async def test_a_command_that_only_needs_a_temp_file_counts_as_read_only(self, tmp_path) -> None:
        """Piped into `tail`, a probe with no temp folder exited 0 and passed off its failure as the result."""
        tool = self._tool()  # a rejecting store: it must never be asked

        command = f"{self.NEEDS_A_SCRATCH_FILE} | tail -1"
        out = await tool.execute(ToolInput(params={"command": command, "workspace": str(tmp_path)}))

        assert out.success and out.data["stdout"].strip() == "done"

    @pytest.mark.asyncio
    async def test_the_probes_temp_folder_is_the_only_new_place_it_can_write(self, tmp_path) -> None:
        tool = self._tool()

        out = await tool.execute(ToolInput(params={"command": "touch /tmp/north-probe-x", "workspace": str(tmp_path)}))

        assert out.failure_kind == "refused"

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

    # Pytest's temp folders are always writable under the sandbox, so where a command
    # may write is checked in folders outside them.
    @pytest.fixture
    def shared(self):
        import shutil
        import uuid
        from pathlib import Path

        folder = Path("/Users/Shared") / f"north-sandbox-test-{uuid.uuid4().hex[:8]}"
        (folder / "task").mkdir(parents=True)
        (folder / "elsewhere").mkdir()
        yield folder
        shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _approved() -> BashTool:
        return bind_approvals(BashTool(os_sandbox=True), store=approving_store())

    @pytest.mark.asyncio
    async def test_the_model_cannot_move_where_a_command_writes(self, shared) -> None:
        """The task's folder is granted by north; a `workspace` outside it used to become writable."""
        task, elsewhere = str(shared / "task"), str(shared / "elsewhere")

        widened = await self._approved().execute(
            ToolInput(params={"command": "touch made.txt", "workspace": elsewhere}, granted_workspace=task)
        )
        inside = await self._approved().execute(
            ToolInput(params={"command": "touch made.txt", "workspace": task}, granted_workspace=task)
        )

        assert not widened.success and not (shared / "elsewhere" / "made.txt").exists()
        assert inside.success and (shared / "task" / "made.txt").exists()

    @pytest.mark.asyncio
    async def test_writing_outside_the_folder_works_once_the_folder_is_approved(self, shared) -> None:
        task, target = str(shared / "task"), shared / "elsewhere" / "x"
        command = f"touch {target}"

        refused = await self._approved().execute(ToolInput(params={"command": command}, granted_workspace=task))
        approved = await self._approved().execute(
            ToolInput(params={"command": command, "write_outside": [str(target.parent)]}, granted_workspace=task)
        )

        assert not refused.success and "write_outside" in refused.error, "it says how to ask"
        assert approved.success and target.exists()

    @pytest.mark.asyncio
    async def test_a_credential_folder_is_unwritable_even_inside_the_granted_folder(self, tmp_path, monkeypatch):
        """Chat tasks ran in the home folder, where an approved command could write into ~/.ssh."""
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))

        out = await self._approved().execute(
            ToolInput(params={"command": f"touch {home}/.ssh/authorized_keys"}, granted_workspace=str(home))
        )

        assert not out.success and not (home / ".ssh" / "authorized_keys").exists()

    @pytest.mark.asyncio
    async def test_a_task_folder_inside_north_home_is_writable_by_its_own_command_only(self) -> None:
        """~/.north is closed to commands; its task folders are readable, as to north's file policy.

        Since issue #78 the shell is north's only file reader, so it reads what `read_file`
        could. Writing stays limited to the task's own folder.
        """
        from utils.handoff import ensure_handoff_dir

        mine, other = ensure_handoff_dir("task_mine"), ensure_handoff_dir("task_other")
        (Path(other) / "notes.txt").write_text("from another task")
        tool = self._approved()

        own = await tool.execute(
            ToolInput(
                params={"command": "echo hi > note.txt && cat note.txt", "workspace": mine}, granted_workspace=mine
            )
        )
        read_other = await tool.execute(
            ToolInput(params={"command": f"cat {other}/notes.txt", "workspace": mine}, granted_workspace=mine)
        )
        write_other = await tool.execute(
            ToolInput(params={"command": f"echo x > {other}/notes.txt", "workspace": mine}, granted_workspace=mine)
        )

        assert own.success and own.data["stdout"].strip() == "hi"
        assert read_other.success and read_other.data["stdout"].strip() == "from another task"
        assert not write_other.success and (Path(other) / "notes.txt").read_text() == "from another task"

    @pytest.mark.asyncio
    async def test_norths_notes_are_readable_and_its_databases_and_secrets_are_not(self) -> None:
        """Notes are read back with the shell; north's state and keys never are."""
        from config.settings import settings
        from tools._path import personal_dir

        notes = Path(personal_dir("notes"))
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "plan.md").write_text("week plan")
        (notes / "state.db").write_text("rows")
        settings.north_home.mkdir(parents=True, exist_ok=True)
        (settings.north_home / "secret.key").write_text("key")
        tool = self._tool()

        plan = await tool.execute(ToolInput(params={"command": f"cat {notes}/plan.md"}))
        database = await tool.execute(ToolInput(params={"command": f"cat {notes}/state.db"}))
        secret = await tool.execute(ToolInput(params={"command": f"cat {settings.north_home}/secret.key"}))

        assert plan.success and plan.data["stdout"] == "week plan"
        assert not database.success and not secret.success

    @pytest.mark.asyncio
    async def test_shell_start_up_files_and_gits_config_are_never_writable(self, tmp_path) -> None:
        """A change there runs code later, outside any sandbox; write_file can still make it, on a card."""
        import subprocess

        tool = self._approved()
        for target in (".zshrc", ".bashrc", ".gitconfig"):
            out = await tool.execute(
                ToolInput(params={"command": f"echo x > {target}"}, granted_workspace=str(tmp_path))
            )
            assert not out.success and "write_file" in (out.error or ""), target
            assert not (tmp_path / target).exists(), target
        ordinary = await tool.execute(
            ToolInput(params={"command": "echo x > notes.txt"}, granted_workspace=str(tmp_path))
        )
        assert ordinary.success

        # git itself may run in a repository, but not change its config, which can name a program git runs.
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        before = (repo / ".git" / "config").read_text()
        out = await tool.execute(
            ToolInput(
                params={"command": "git config core.pager evil", "workspace": str(repo)}, granted_workspace=str(repo)
            )
        )
        assert not out.success and (repo / ".git" / "config").read_text() == before

    def test_git_hooks_are_closed_to_writes_in_the_profile(self, tmp_path) -> None:
        """A repository's own policy refuses most edits there first; the kernel rule holds for whatever is left."""
        from tools.specialized._seatbelt import profile

        assert r'(regex #"/\.git/hooks(/|$)")' in profile(str(tmp_path), writable=True)

    @pytest.mark.asyncio
    async def test_a_long_output_is_kept_whole(self, tmp_path) -> None:
        """Cut at 30,000 characters, the rest was lost before the agent's overflow store could keep it."""
        tool = self._tool()

        out = await tool.execute(ToolInput(params={"command": "seq 1 20000"}, granted_workspace=str(tmp_path)))

        assert out.success and out.data["stdout"].splitlines()[-1] == "20000"
        assert len(out.data["stdout"]) > 30_000

    @pytest.mark.asyncio
    async def test_the_read_only_run_gets_the_commands_own_time(self, tmp_path, monkeypatch) -> None:
        """With 10 seconds, a slow read such as a large `find` went to a card although it changed nothing."""
        seen: list[int] = []
        original = BashTool._spawn

        async def spawn(self, argv, cwd, timeout, **kwargs):
            seen.append(timeout)
            return await original(self, argv, cwd, timeout, **kwargs)

        monkeypatch.setattr(BashTool, "_spawn", spawn)

        out = await self._tool().execute(
            ToolInput(params={"command": "true", "timeout": 120}, granted_workspace=str(tmp_path))
        )

        assert out.success and seen == [120], "one run, read-only, with the time the call asked for"

    def test_docker_replaces_the_os_sandbox(self) -> None:
        from tools.specialized._sandbox import SandboxConfig

        assert BashTool(sandbox=SandboxConfig(enabled=True), os_sandbox=True)._os_sandbox is None


def test_a_platform_with_no_sandbox_asks_for_every_command(monkeypatch) -> None:
    monkeypatch.setattr(_os_sandbox, "_IMPLEMENTATIONS", ())

    assert BashTool(os_sandbox=True)._os_sandbox is None


@pytest.mark.parametrize(("isolated_copy", "longest"), [(True, VERIFY_TIMEOUT_SECONDS), (False, 300)])
async def test_only_norths_own_test_run_may_take_longer_than_five_minutes(
    tmp_path: Path, monkeypatch, isolated_copy: bool, longest: int
) -> None:
    """Seaborn's suite was cut off at 300 s; a model's own command keeps that cap."""
    seen: list[int] = []

    async def spawn(self, argv, cwd, timeout, **_):
        seen.append(timeout)
        return ToolOutput(success=True, data={"returncode": 0, "stdout": "", "stderr": ""})

    monkeypatch.setattr(BashTool, "_spawn", spawn)
    tool = bind_approvals(BashTool(os_sandbox=False), store=approving_store())

    await tool.execute(
        ToolInput(
            params={"command": "pytest -q", "workspace": str(tmp_path), "timeout": 10_000},
            granted_workspace=str(tmp_path),
            isolated_copy=isolated_copy,
        )
    )

    assert seen == [longest]
