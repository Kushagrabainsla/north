"""The gate: what north decides about each thing a coding agent is about to do.

The agent's own permission system is default-deny; a PreToolUse hook (`hook.py`) asks the daemon,
and the answer is one of three. ALLOW and DENY are explicit. PASS says nothing, so the vendor
decides - north only passes what the vendor already runs on its own (a plainly read-only shell
command), so a pass can never grant anything. Everything here is pure; the policy that rules on
an action lives in the approval layer and arrives through `Judge`.
"""

from __future__ import annotations

import os
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PASS = "pass"  # no decision: the vendor's own rules apply


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


@dataclass(frozen=True)
class ToolRequest:
    """One tool call the agent is about to make, read from the hook's payload."""

    tool: str
    command: str = ""
    path: str = ""
    url: str = ""


@dataclass(frozen=True)
class GateSession:
    """What a run's token stands for. The worktree is north's, never taken from the payload."""

    token: str
    run_id: str
    task_id: str
    worktree: str


class GateSessions:
    """The tokens of the runs in flight."""

    def __init__(self) -> None:
        self._sessions: dict[str, GateSession] = {}

    def issue(self, run_id: str, task_id: str, worktree: str) -> GateSession:
        session = GateSession(secrets.token_urlsafe(32), run_id, task_id, worktree)
        self._sessions[session.token] = session
        return session

    def lookup(self, token: str) -> GateSession | None:
        return self._sessions.get(token)

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)


class Judge(Protocol):
    """Rules on a request that is not plainly safe. The approval layer implements it."""

    async def judge(self, session: GateSession, request: ToolRequest, *, inside_worktree: bool) -> Verdict: ...


# Tools that write files, and the field that names the file.
FILE_TOOLS: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
# The tools the hook is asked about. Any other tool that needs approval is denied by default.
HOOKED_TOOLS = "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__.*"


class Gate:
    """Reads a hook payload, handles what north can settle by itself, and hands the rest to the judge."""

    def __init__(self, judge: Judge) -> None:
        self._judge = judge

    async def decide(self, session: GateSession, payload: Mapping[str, Any]) -> Verdict:
        request = parse_hook_payload(payload)
        if request is None:
            return Verdict(Decision.DENY, "north could not read this tool call")
        if request.tool == "Bash" and is_plainly_read_only(request.command):
            return Verdict(Decision.PASS, "a read-only command")
        inside = False
        if request.tool in FILE_TOOLS:
            target = resolve_path(session.worktree, request.path)
            if _touches_git_dir(target, session.worktree):
                return Verdict(Decision.DENY, "a run may not change git's own files")
            request = replace(request, path=str(target))
            inside = is_inside(session.worktree, target)
        return await self._judge.judge(session, request, inside_worktree=inside)


def parse_hook_payload(payload: Mapping[str, Any]) -> ToolRequest | None:
    """The tool call in a PreToolUse payload, or None when it is not one."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if payload.get("hook_event_name") != "PreToolUse" or not isinstance(tool, str) or not isinstance(tool_input, dict):
        return None
    return ToolRequest(
        tool=tool,
        command=str(tool_input.get("command") or ""),
        path=str(tool_input.get(FILE_TOOLS.get(tool, ""), "") or ""),
        url=str(tool_input.get("url") or ""),
    )


def resolve_path(worktree: str, path: str) -> Path:
    """Where *path* really lands: relative to the worktree, with `..` and symlinks followed."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(worktree) / candidate
    return Path(os.path.realpath(candidate))


def is_inside(worktree: str, target: Path) -> bool:
    root = Path(os.path.realpath(worktree))
    return target == root or root in target.parents


def _touches_git_dir(target: Path, worktree: str) -> bool:
    """A linked worktree's `.git` is a file that points at the real repository; changing it, or
    anything under a `.git` directory, could redirect later git commands."""
    root = Path(os.path.realpath(worktree))
    parts = target.relative_to(root).parts if is_inside(worktree, target) else target.parts
    return ".git" in parts


_SHELL_SYNTAX = frozenset(";&|<>`$\n\\(){}")
_READ_ONLY_PROGRAMS = frozenset(
    {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "tree", "stat", "file", "which"}
)
_READ_ONLY_GIT = frozenset({"status", "log", "diff", "show", "ls-files", "rev-parse", "blame"})
# Flags that make an otherwise read-only program write or run something.
_WRITING_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "--output", "-o", "--ext-diff"})


def is_plainly_read_only(command: str) -> bool:
    """True only for a single, simple, read-only command. Anything doubtful is False, which only means
    "ask": the vendor still has the last word on whether a passed command runs."""
    if not command.strip() or any(char in _SHELL_SYNTAX for char in command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    program, args = words[0], words[1:]
    if program == "git":
        return bool(args) and args[0] in _READ_ONLY_GIT and not _writes(args[1:])
    if program == "find":
        return not _writes(args)
    return program in _READ_ONLY_PROGRAMS and not _writes(args)


def _writes(args: list[str]) -> bool:
    return any(arg in _WRITING_FLAGS or arg.startswith("--output=") for arg in args)
