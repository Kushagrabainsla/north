"""Tests for in-code approval gates on GitTool/GhTool (review findings R2#10, R2#11, R4#25)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config.approval_mode import ApprovalMode
from tests.conftest import bind_approvals, deciding
from tools.models import ToolInput, ToolOutput
from tools.specialized import gh_tool as gh_module
from tools.specialized import git_tool as git_module
from tools.specialized.gh_tool import GhTool
from tools.specialized.git_tool import GitTool

_AUTONOMOUS = ApprovalMode.AUTONOMOUS


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


# ── GitTool ──────────────────────────────────────────────────────────────────


class TestGitGate:
    async def test_readonly_actions_run_without_approval(self, fake_run_capture) -> None:
        result = await GitTool().execute(ToolInput(params={"action": "status"}))
        assert result.success is True
        assert fake_run_capture, "git status should have executed"

    async def test_branch_listing_runs_without_approval(self, fake_run_capture) -> None:
        result = await GitTool().execute(ToolInput(params={"action": "branch", "args": "-a"}))
        assert result.success is True

    @pytest.mark.parametrize("action,args", [("commit", "msg"), ("push", ""), ("merge", "x"), ("add", ".")])
    async def test_mutating_actions_fail_closed_without_gate(self, fake_run_capture, action, args) -> None:
        result = await GitTool().execute(ToolInput(params={"action": action, "args": args}))
        assert result.success is False
        assert "fail closed" in result.error
        assert not fake_run_capture, "no subprocess may run without an approval gate"

    async def test_branch_create_fails_closed_without_gate(self, fake_run_capture) -> None:
        result = await GitTool().execute(ToolInput(params={"action": "branch", "args": "-D main"}))
        assert result.success is False
        assert not fake_run_capture

    async def test_mutating_action_runs_when_user_approves(self, fake_run_capture) -> None:
        tool = bind_approvals(GitTool(), store=_approving_store())
        result = await tool.execute(ToolInput(params={"action": "commit", "args": "fix: things"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["git", "commit", "-m"]

    async def test_mutating_action_refused_when_user_rejects(self, fake_run_capture) -> None:
        tool = bind_approvals(GitTool(), store=_rejecting_store())
        result = await tool.execute(ToolInput(params={"action": "push", "args": "origin main"}))
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
        tool = bind_approvals(GitTool(), store=_approving_store())
        result = await tool.execute(ToolInput(params={"action": "push", "args": args}))
        assert result.success is False
        assert "blocked" in result.error.lower() and "new branch" in result.error
        assert not fake_run_capture

    async def test_force_push_allowed_when_allow_dangerous(self, fake_run_capture) -> None:
        """Autonomous has no hard floor: the memory decider may approve a force push."""
        tool = bind_approvals(GitTool(), _AUTONOMOUS, store=_approving_store(), decider=deciding("approved"))
        result = await tool.execute(ToolInput(params={"action": "push", "args": "origin main --force"}))
        assert result.success is True
        assert fake_run_capture  # it actually ran (after approval), not pre-blocked

    async def test_reset_and_clean_are_not_offered(self, fake_run_capture) -> None:
        for action in ("reset", "clean"):
            result = await GitTool().execute(ToolInput(params={"action": action, "args": "--hard"}))
            assert result.success is False
            assert "Unknown git action" in result.error


# ── GhTool ───────────────────────────────────────────────────────────────────


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


# ── read-only argument allowlist ─────────────────────────────────────────────


class TestReadOnlyArgumentAllowlist:
    """Read-only git skips the approval card, so its arguments are the last gate.

    `git diff --output=<path>` writes a file at any absolute path - outside the
    workspace and outside `tools/_path.py` - with no approval card shown, which
    made every read-only action an unsupervised write primitive.
    """

    @pytest.mark.parametrize(
        "action,args",
        [
            ("diff", "--output=/tmp/escape"),
            ("diff", "--output /tmp/escape"),
            ("show", "--output=/tmp/escape"),
            ("log", "-p --output=/tmp/escape"),
            ("diff", "--ext-diff"),
            ("diff", "--textconv"),
            ("diff", "-O/tmp/orderfile"),
        ],
    )
    async def test_filesystem_touching_options_are_refused(self, fake_run_capture, action, args) -> None:
        result = await GitTool().execute(ToolInput(params={"action": action, "args": args}))
        assert result.success is False
        assert "not permitted" in result.error
        assert not fake_run_capture, "no subprocess may run for a refused option"

    @pytest.mark.parametrize(
        "action,args",
        [
            ("diff", "--stat"),
            ("diff", "--cached --name-only"),
            ("diff", "-U3"),
            ("diff", "--unified=3"),
            ("diff", "HEAD~1 HEAD"),
            ("diff", "-- src/some-file.py"),
            ("log", "-20"),
            ("log", "-n 5 --author=me"),
            ("show", "HEAD"),
            ("show", "--stat HEAD"),
            ("status", ""),
        ],
    )
    async def test_ordinary_read_only_usage_still_runs(self, fake_run_capture, action, args) -> None:
        result = await GitTool().execute(ToolInput(params={"action": action, "args": args}))
        assert result.success is True
        assert fake_run_capture, f"`git {action} {args}` should have executed"

    async def test_pathspec_after_separator_is_not_read_as_an_option(self, fake_run_capture) -> None:
        """A file legitimately named like a flag is a pathspec once `--` is seen."""
        result = await GitTool().execute(ToolInput(params={"action": "diff", "args": "-- --output=notaflag"}))
        assert result.success is True
        assert fake_run_capture

    async def test_allowlist_does_not_gate_mutating_actions(self, fake_run_capture) -> None:
        """Mutating actions keep going through the approval card, which shows the
        full command - the allowlist is only for the ungated read-only path."""
        tool = bind_approvals(GitTool(), store=_approving_store())
        result = await tool.execute(ToolInput(params={"action": "checkout", "args": "-b feature/x"}))
        assert result.success is True
        assert fake_run_capture[0][:3] == ["git", "checkout", "-b"]


class TestBranchListPattern:
    """`git branch --list <pattern>` filters the listing; it cannot create."""

    @pytest.mark.parametrize("args", ["--list north/task_abc", "-l north/*", "--list"])
    async def test_listing_with_a_pattern_needs_no_approval(self, fake_run_capture, args) -> None:
        result = await GitTool().execute(ToolInput(params={"action": "branch", "args": args}))
        assert result.success is True
        assert fake_run_capture, f"`git branch {args}` is read-only"

    @pytest.mark.parametrize("args", ["newbranch", "-v newbranch", "-d gone", "-m old new"])
    async def test_naming_a_branch_without_list_still_needs_approval(self, fake_run_capture, args) -> None:
        result = await GitTool().execute(ToolInput(params={"action": "branch", "args": args}))
        assert result.success is False
        assert not fake_run_capture


class TestRejectedApproval:
    """A person saying no is reported as exactly that - a refusal, not a broken tool."""

    async def test_a_real_rejection_still_says_rejected(self, fake_run_capture) -> None:
        tool = bind_approvals(GitTool(), store=_rejecting_store())
        result = await tool.execute(ToolInput(params={"action": "commit", "args": "wip"}))
        assert result.success is False
        assert result.error == "Git operation rejected by user."
        assert result.failure_kind == "refused"


class TestWhatLeavesTheSandbox:
    """Autonomous approves these only when something you stated covers them (approval/decider.py)."""

    @pytest.mark.parametrize(
        ("action", "args", "leaves"),
        [("push", "origin main", True), ("commit", "-m msg", False), ("add", ".", False), ("status", "", False)],
    )
    async def test_only_a_push_leaves_the_sandbox(self, fake_run_capture, action, args, leaves) -> None:
        request = await GitTool().describe(ToolInput(params={"action": action, "args": args}))

        if request is not None:  # a read-only call has nothing to ask about, so nothing to flag
            assert request.action.leaves_sandbox is leaves

    async def test_a_mutating_github_call_leaves_the_sandbox_and_a_read_does_not(self, fake_run_capture) -> None:
        write = await GhTool().describe(ToolInput(params={"action": "pr_create", "args": "--title x"}))
        read = await GhTool().describe(ToolInput(params={"action": "pr_list"}))

        assert write.action.leaves_sandbox is True
        assert read.action.leaves_sandbox is False
