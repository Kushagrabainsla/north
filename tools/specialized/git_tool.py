"""GitTool - structured git operations for engineering agents.

Safe read-only operations (status, diff, log, show, and listing branches)
execute immediately. Mutating operations (add, commit, push, pull, checkout,
stash, merge, branch create/delete) are gated in code behind a user approval
card - the gate does not rely on the agent's system prompt. Force pushes are
permanently blocked via token-level argument parsing. reset/clean are not
offered as actions at all.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any

from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_mutating_action
from tools.specialized._subprocess import format_diff_output, run_capture

_TIMEOUT = 30

# Actions that never change repository state. Everything else is mutating and
# requires in-code approval before the subprocess is spawned.
_READONLY_ACTIONS: frozenset[str] = frozenset({"status", "diff", "log", "show"})
# `branch` is read-only only when listing; these flags keep it on the fast path.
_BRANCH_LIST_FLAGS: frozenset[str] = frozenset({"-a", "-r", "-v", "-vv", "--list", "--all", "--show-current"})


def _is_force_flag(token: str) -> bool:
    """Token-level force detection - robust against `--force-with-lease=ref` etc."""
    return token == "-f" or token.startswith("--force")


class GitTool(ApprovalGatedTool):
    """Run git commands with structured output and safety guards."""

    name = "git"
    is_mutating = True
    description = (
        "Run git operations in the workspace. "
        "Read-only actions (status, diff, log, show, branch listing) execute immediately. "
        "Mutating actions (add, commit, push, pull, checkout, stash, merge, branch create/delete) "
        "automatically show the user an approval card before running - no separate "
        "request_approval call is needed. Force-push is always blocked; reset/clean are not available."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "log",
                    "branch",
                    "show",
                    "add",
                    "commit",
                    "push",
                    "pull",
                    "checkout",
                    "stash",
                    "merge",
                ],
                "description": "Git action to perform",
            },
            "args": {
                "type": "string",
                "description": (
                    "Extra arguments passed to the git command. "
                    "For commit: the commit message string. "
                    "For add: paths to stage (default '.'). "
                    "For checkout: branch name. "
                    "For diff/log/show: optional path or ref."
                ),
                "default": "",
            },
            "workspace": {
                "type": "string",
                "description": "Repository root directory (defaults to CWD)",
            },
        },
        "required": ["action"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        stdout = str(data.get("stdout", "")).strip()
        command = str(data.get("command", ""))
        if "diff" in command:
            return format_diff_output(stdout)
        return stdout

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action", "")).strip()
        args = str(input.params.get("args", "")).strip()
        workspace = input.params.get("workspace") or None
        cwd = Path(workspace).resolve() if workspace else Path.cwd()

        if not shutil.which("git"):
            return ToolOutput(success=False, error="git is not installed or not in PATH.")

        try:
            cmd = _build_command(action, args)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Could not parse args: {exc}")
        if cmd is None:
            return ToolOutput(
                success=False,
                error=f"Unknown git action: {action!r}. "
                f"Valid: status, diff, log, branch, show, add, commit, push, pull, checkout, stash, merge.",
            )

        # Force pushes are permanently blocked - token-level so quoting or flag
        # reordering cannot slip past a prefix match.
        if action == "push" and any(_is_force_flag(t) for t in cmd[2:]):
            return ToolOutput(
                success=False,
                error="Force-push is permanently blocked - too destructive. Push to a new branch instead.",
            )

        if _is_mutating(action, cmd):
            denial = await gate_mutating_action(
                self._approval_store,
                agent="git",
                title="Git Operation - Approval Required",
                message=f"```\n{' '.join(cmd)}\n```",
                task_id=input.params.get("task_id"),
                stream_manager=self._stream_manager,
                judgement_filter=self._judgement_filter,
                timeout=self._approval_timeout_seconds,
            )
            if denial is not None:
                return ToolOutput(success=False, error=denial)

        return await asyncio.to_thread(run_capture, cmd, cwd, timeout=_TIMEOUT)


def _is_mutating(action: str, cmd: list[str]) -> bool:
    if action in _READONLY_ACTIONS:
        return False
    if action == "branch":
        # Listing branches is read-only; any non-list flag or positional
        # argument (create/delete/rename) makes it mutating.
        return any(token not in _BRANCH_LIST_FLAGS for token in cmd[2:])
    return True


def _build_command(action: str, args: str) -> list[str] | None:
    base: list[str] = ["git"]
    # shlex.split honours quoting so paths/refs containing spaces survive intact;
    # a malformed quote raises ValueError, surfaced as a tool error by the caller.
    arg_parts = shlex.split(args) if args else []

    match action:
        case "status":
            return base + ["status", "--short", "--branch"]
        case "diff":
            return base + ["diff"] + arg_parts
        case "log":
            return base + ["log", "--oneline", "--graph", "--decorate", "-20"] + arg_parts
        case "branch":
            return base + ["branch"] + arg_parts
        case "show":
            return base + ["show"] + (arg_parts or ["HEAD"])
        case "add":
            return base + ["add"] + (arg_parts or ["."])
        case "commit":
            if not args:
                return None  # message is required
            return base + ["commit", "-m", args]
        case "push":
            return base + ["push"] + arg_parts
        case "pull":
            return base + ["pull"] + arg_parts
        case "checkout":
            return base + ["checkout"] + arg_parts
        case "stash":
            return base + ["stash"] + arg_parts
        case "merge":
            return base + ["merge"] + arg_parts
        case _:
            return None
