"""GhTool - GitHub CLI operations for engineering agents.

Mirrors GitTool's safety model: read-only actions (pr view/diff/list/checks,
issue view/list, repo view, run list/view) execute immediately; mutating
actions (pr create/comment/merge/review, issue create/comment) are gated in
code behind a user approval card - the gate does not rely on the agent's
system prompt.

Authentication is delegated to the ``gh`` CLI itself (``gh auth login``); this
tool never handles tokens. Prefer this over a GitHub MCP server for GitHub ops.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from tools.specialized._subprocess import format_diff_output, run_capture

_TIMEOUT = 30

# action → gh subcommand parts. Args from the model are appended (shlex-split).
_ACTIONS: dict[str, list[str]] = {
    "pr_view": ["pr", "view"],
    "pr_diff": ["pr", "diff"],
    "pr_list": ["pr", "list"],
    "pr_status": ["pr", "status"],
    "pr_checks": ["pr", "checks"],
    "pr_create": ["pr", "create"],
    "pr_ready": ["pr", "ready"],
    "pr_comment": ["pr", "comment"],
    "pr_merge": ["pr", "merge"],
    "pr_review": ["pr", "review"],
    "issue_view": ["issue", "view"],
    "issue_list": ["issue", "list"],
    "issue_create": ["issue", "create"],
    "issue_comment": ["issue", "comment"],
    "repo_view": ["repo", "view"],
    "run_list": ["run", "list"],
    "run_view": ["run", "view"],
}

# Actions that write to GitHub. Each one is gated behind in-code user approval
# (fail-closed when no ApprovalStore is wired). pr_merge in particular must
# never execute silently.
_MUTATING_ACTIONS: frozenset[str] = frozenset(
    {"pr_create", "pr_ready", "pr_comment", "pr_merge", "pr_review", "issue_create", "issue_comment"}
)


@dataclass(frozen=True)
class _GhCall:
    """A checked gh command, ready to describe or run."""

    action: str
    args: str
    cmd: list[str]
    cwd: Path


class GhTool(Tool):
    """Run GitHub CLI operations with structured output and safety guards."""

    name = "gh"
    is_mutating = True
    description = (
        "Run GitHub operations via the gh CLI. "
        "Read-only actions (pr_view, pr_diff, pr_list, pr_status, pr_checks, issue_view, "
        "issue_list, repo_view, run_list, run_view) execute immediately. Use pr_checks to read "
        "a PR's CI status and run_view (args='<run-id> --log-failed') to fetch failing CI logs. "
        "Mutating actions (pr_create, pr_ready, pr_comment, pr_merge, pr_review, issue_create, "
        "issue_comment) automatically show the user an approval card before running - "
        "no separate request_approval call is needed. "
        "Pass extra flags via 'args', e.g. args='123 --body \"LGTM\"'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS.keys()),
                "description": "GitHub action to perform",
            },
            "args": {
                "type": "string",
                "description": (
                    "Extra arguments appended to the gh command, quoted as on a shell. "
                    "E.g. for pr_comment: '123 --body \"Looks good\"'. "
                    "For pr_view/pr_diff: a PR number or branch (defaults to current branch)."
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
        if "pr/diff" in str(data.get("command", "")) or "pr diff" in str(data.get("command", "")):
            return format_diff_output(stdout)
        return stdout or "(no output)"

    async def describe(self, input: ToolInput) -> Request | None:
        call = self._prepare(input)
        if isinstance(call, ToolOutput):
            return None
        return Request(
            action=Action(
                agent="gh",
                kind=ActionKind.GITHUB,
                summary=" ".join(call.cmd),
                operation=call.action,
                args=call.args,
                mutating=call.action in _MUTATING_ACTIONS,
                read_only=call.action not in _MUTATING_ACTIONS,
            ),
            title="GitHub Operation - Approval Required",
            message=f"```\n{' '.join(call.cmd)}\n```",
            declined="GitHub operation rejected by user.",
            prepared=call,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        call = prepared(input) or self._prepare(input)
        if isinstance(call, ToolOutput):
            return call
        return await asyncio.to_thread(run_capture, call.cmd, call.cwd, timeout=_TIMEOUT)

    @staticmethod
    def _prepare(input: ToolInput) -> _GhCall | ToolOutput:
        """Check the call and build its command, or say why it cannot run."""
        action = str(input.params.get("action", "")).strip()
        args = str(input.params.get("args", "")).strip()
        workspace = input.params.get("workspace") or None
        cwd = Path(workspace).resolve() if workspace else Path.cwd()

        if not shutil.which("gh"):
            return ToolOutput(
                success=False,
                error="gh (GitHub CLI) is not installed or not in PATH. Install from https://cli.github.com.",
            )
        if action not in _ACTIONS:
            return ToolOutput(
                success=False,
                error=f"Unknown gh action: {action!r}. Valid: {', '.join(sorted(_ACTIONS))}.",
            )
        try:
            arg_parts = shlex.split(args) if args else []
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Could not parse args: {exc}")
        return _GhCall(action, args, ["gh", *_ACTIONS[action], *arg_parts], cwd)
