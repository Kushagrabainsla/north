"""The gate's own logic: what it settles itself, and what it hands to the judge."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agents.gate import (
    Decision,
    Gate,
    GateSessions,
    ToolRequest,
    Verdict,
    is_inside,
    is_plainly_read_only,
    parse_hook_payload,
    resolve_path,
)


def _payload(tool: str, **tool_input) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": "/anywhere"}


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    return root


class RecordingJudge:
    def __init__(self, verdict: Verdict | None = None) -> None:
        self.verdict = verdict or Verdict(Decision.ALLOW, "approved")
        self.calls: list[tuple[ToolRequest, bool]] = []

    async def judge(self, session, request, *, inside_worktree):
        self.calls.append((request, inside_worktree))
        return self.verdict


class TestReadingThePayload:
    def test_a_shell_command_and_a_file_write_are_read_by_their_own_fields(self) -> None:
        assert parse_hook_payload(_payload("Bash", command="make test")) == ToolRequest("Bash", command="make test")
        assert parse_hook_payload(_payload("Write", file_path="a.py", content="x")).path == "a.py"
        assert parse_hook_payload(_payload("NotebookEdit", notebook_path="n.ipynb")).path == "n.ipynb"
        assert parse_hook_payload(_payload("WebFetch", url="https://example.com")).url == "https://example.com"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}},
            _payload("Bash") | {"tool_input": "x"},
        ],
    )
    def test_anything_that_is_not_a_tool_call_is_unreadable(self, payload) -> None:
        assert parse_hook_payload(payload) is None


class TestWhereAPathLands:
    def test_a_relative_path_is_inside_the_worktree(self, worktree) -> None:
        target = resolve_path(str(worktree), "src/app.py")

        assert is_inside(str(worktree), target) and target.name == "app.py"

    @pytest.mark.parametrize("path", ["../escape.txt", "src/../../escape.txt", "/etc/hosts", "~/escape.txt"])
    def test_a_path_that_climbs_out_is_outside(self, worktree, path) -> None:
        assert not is_inside(str(worktree), resolve_path(str(worktree), path))

    def test_a_symlink_that_points_out_does_not_hide_where_the_write_lands(self, worktree, tmp_path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, worktree / "link")

        target = resolve_path(str(worktree), "link/new.txt")

        assert target == (outside / "new.txt").resolve() and not is_inside(str(worktree), target)

    def test_a_file_that_does_not_exist_yet_is_judged_by_where_it_would_be(self, worktree) -> None:
        assert is_inside(str(worktree), resolve_path(str(worktree), "new/dir/file.py"))


class TestWhatIsPlainlyReadOnly:
    @pytest.mark.parametrize(
        "command",
        [
            "ls",
            "ls -la src",
            "pwd",
            "cat README.md",
            "head -n 5 a.py",
            "grep -rn TODO src",
            "git status",
            "git log --oneline -5",
            "git diff HEAD~1",
            "find . -name '*.py'",
            "tree -L 2",
            "wc -l a.py",
        ],
    )
    def test_these_are_passed_to_the_vendor(self, command) -> None:
        assert is_plainly_read_only(command)

    @pytest.mark.parametrize(
        "command",
        [
            "",
            "ls; rm -rf .",
            "ls && touch x",
            "cat a | sh",
            "cat a > b",
            "echo $(whoami)",
            "ls `pwd`",
            "git push origin main",
            "git commit -m x",
            "git diff --output=out.txt",
            "git -c core.pager=x log",
            "find . -exec rm {} ;",
            "find . -delete",
            "sort -o out.txt a",
            "touch x",
            "make test",
            "cat a\nrm b",
            "ls $HOME",
            "grep 'unterminated",
        ],
    )
    def test_anything_that_could_write_or_chain_is_not(self, command) -> None:
        assert not is_plainly_read_only(command)


class TestWhatTheGateSettlesItself:
    async def _decide(self, worktree, payload, judge=None):
        sessions = GateSessions()
        session = sessions.issue("run-1", "t1", str(worktree))
        return await Gate(judge or RecordingJudge()).decide(session, payload)

    async def test_a_read_only_command_is_passed_without_bothering_the_judge(self, worktree) -> None:
        judge = RecordingJudge()

        verdict = await self._decide(worktree, _payload("Bash", command="git status"), judge)

        assert verdict.decision is Decision.PASS and not judge.calls

    async def test_a_command_that_changes_something_goes_to_the_judge(self, worktree) -> None:
        judge = RecordingJudge(Verdict(Decision.ALLOW, "you said yes"))

        verdict = await self._decide(worktree, _payload("Bash", command="make test"), judge)

        assert verdict == Verdict(Decision.ALLOW, "you said yes")
        assert judge.calls[0][0].command == "make test"

    async def test_a_write_inside_the_worktree_is_flagged_inside_with_its_real_path(self, worktree) -> None:
        judge = RecordingJudge()

        await self._decide(worktree, _payload("Write", file_path="src/a.py"), judge)

        request, inside = judge.calls[0]
        assert inside and Path(request.path) == (worktree / "src" / "a.py").resolve()

    async def test_a_write_outside_the_worktree_still_goes_to_the_judge_flagged_outside(self, worktree) -> None:
        judge = RecordingJudge(Verdict(Decision.DENY, "no"))

        verdict = await self._decide(worktree, _payload("Write", file_path="../escape.txt"), judge)

        assert verdict.decision is Decision.DENY
        assert judge.calls[0][1] is False

    @pytest.mark.parametrize("path", [".git", ".git/config", "sub/.git/hooks/pre-commit"])
    async def test_git_s_own_files_are_never_changed_and_nobody_is_asked(self, worktree, path) -> None:
        judge = RecordingJudge()

        verdict = await self._decide(worktree, _payload("Edit", file_path=path), judge)

        assert verdict.decision is Decision.DENY and not judge.calls

    async def test_a_payload_it_cannot_read_is_denied(self, worktree) -> None:
        verdict = await self._decide(worktree, {"nonsense": True})

        assert verdict.decision is Decision.DENY


class TestSessions:
    def test_each_run_gets_its_own_token_and_it_stops_working_when_revoked(self) -> None:
        sessions = GateSessions()
        first = sessions.issue("run-1", "t1", "/wt1")
        second = sessions.issue("run-2", "t1", "/wt2")

        assert first.token != second.token and sessions.lookup(first.token) == first
        sessions.revoke(first.token)
        assert sessions.lookup(first.token) is None and sessions.lookup(second.token) == second
        assert sessions.lookup("guess") is None


class TestAskingNorth:
    async def test_the_ask_tool_is_passed_so_the_vendors_own_one_allowed_tool_rule_applies(self, worktree) -> None:
        judge = RecordingJudge()
        session = GateSessions().issue("run-1", "t1", str(worktree))

        verdict = await Gate(judge).decide(session, _payload("mcp__north__ask_north", question="tabs?"))

        assert verdict.decision is Decision.PASS and not judge.calls

    async def test_any_other_mcp_tool_still_goes_to_the_judge(self, worktree) -> None:
        judge = RecordingJudge(Verdict(Decision.DENY, "no"))
        session = GateSessions().issue("run-1", "t1", str(worktree))

        verdict = await Gate(judge).decide(session, _payload("mcp__other__thing"))

        assert verdict.decision is Decision.DENY and judge.calls

    async def test_a_read_only_runs_token_has_no_gate_at_all(self, worktree) -> None:
        session = GateSessions().issue("run-1", "t1", str(worktree), editing=False)

        verdict = await Gate(RecordingJudge()).decide(session, _payload("Write", file_path="x.py"))

        assert verdict.decision is Decision.DENY and "read-only" in verdict.reason
