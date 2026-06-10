"""GhTool — GitHub CLI operations for engineering agents.

Mirrors GitTool's safety model: read-only actions (pr view/diff/list/checks,
issue view/list, repo view, run list/view) execute immediately; mutating
actions (pr create/comment/merge/review, issue create/comment) are allowed but
the coder agent's system prompt instructs it to call ``request_approval`` first.

Authentication is delegated to the ``gh`` CLI itself (``gh auth login``); this
tool never handles tokens. Prefer this over a GitHub MCP server for GitHub ops.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized._subprocess import run_capture

_TIMEOUT = 30

# action → gh subcommand parts. Args from the model are appended (shlex-split).
_ACTIONS: dict[str, list[str]] = {
    "pr_view": ["pr", "view"],
    "pr_diff": ["pr", "diff"],
    "pr_list": ["pr", "list"],
    "pr_checks": ["pr", "checks"],
    "pr_create": ["pr", "create"],
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


class GhTool(Tool):
    """Run GitHub CLI operations with structured output and safety guards."""

    name = "gh"
    is_mutating = True
    description = (
        "Run GitHub operations via the gh CLI. "
        "Read-only actions (pr_view, pr_diff, pr_list, pr_checks, issue_view, issue_list, "
        "repo_view, run_list, run_view) execute immediately. "
        "Mutating actions (pr_create, pr_comment, pr_merge, pr_review, issue_create, "
        "issue_comment) require you to call request_approval first. "
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
            return f"```diff\n{stdout}\n```" if stdout else "No diff."
        return stdout or "(no output)"

    async def run(self, input: ToolInput) -> ToolOutput:
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

        cmd = ["gh", *_ACTIONS[action], *arg_parts]
        return await asyncio.to_thread(run_capture, cmd, cwd, timeout=_TIMEOUT)
