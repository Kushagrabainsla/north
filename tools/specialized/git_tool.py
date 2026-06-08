"""GitTool — structured git operations for engineering agents.

Safe read-only operations (status, diff, log, branch, show) execute
immediately.  Write operations (add, commit, push, stash) are allowed but
the coder agent's system prompt instructs it to call ``request_approval``
before any of them.  Truly dangerous operations (force-push, reset --hard,
clean -fdx) are permanently blocked with a clear error message.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_TIMEOUT = 30

# Operations blocked outright — too destructive even with approval.
_ALWAYS_BLOCKED: frozenset[str] = frozenset({
    "push --force",
    "push -f",
    "push --force-with-lease",
    "reset --hard",
    "clean -f",
    "clean -fd",
    "clean -fdx",
})


class GitTool(Tool):
    """Run git commands with structured output and safety guards."""

    name = "git"
    description = (
        "Run git operations in the workspace. "
        "Read-only actions (status, diff, log, branch, show) are safe and execute immediately. "
        "Write actions (add, commit, push, pull, stash, checkout) require you to call "
        "request_approval first. Force-push and reset --hard are always blocked."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status", "diff", "log", "branch", "show",
                    "add", "commit", "push", "pull",
                    "checkout", "stash", "merge",
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
            if not stdout:
                return "No changes (empty diff)."
            lines = stdout.splitlines()
            files_changed = []
            for line in lines:
                if line.startswith("+++ b/"):
                    files_changed.append(line[6:])
            summary = ""
            if files_changed:
                summary = "### Files Modified:\n" + "\n".join(f"- `{f}`" for f in files_changed) + "\n\n"
            return f"{summary}### Diff:\n```diff\n{stdout}\n```"
        return stdout

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action", "")).strip()
        args = str(input.params.get("args", "")).strip()
        workspace = input.params.get("workspace") or None
        cwd = Path(workspace).resolve() if workspace else Path.cwd()

        if not shutil.which("git"):
            return ToolOutput(success=False, error="git is not installed or not in PATH.")

        # Build the full git command.
        cmd = _build_command(action, args)
        if cmd is None:
            return ToolOutput(
                success=False,
                error=f"Unknown git action: {action!r}. "
                      f"Valid: status, diff, log, branch, show, add, commit, push, pull, checkout, stash, merge.",
            )

        # Block permanently dangerous operations.
        cmd_str = " ".join(cmd[1:])  # everything after "git"
        for blocked in _ALWAYS_BLOCKED:
            if cmd_str.startswith(blocked):
                return ToolOutput(
                    success=False,
                    error=(
                        f"'{cmd_str}' is permanently blocked — too destructive. "
                        "Use a targeted reset or revert instead."
                    ),
                )

        return await asyncio.to_thread(_run_sync, cmd, cwd)


def _build_command(action: str, args: str) -> list[str] | None:
    base: list[str] = ["git"]
    arg_parts = args.split() if args else []

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


def _run_sync(cmd: list[str], cwd: Path) -> ToolOutput:
    import subprocess

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error=f"git command timed out after {_TIMEOUT}s.")
    except FileNotFoundError:
        return ToolOutput(success=False, error="git executable not found.")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # Cap output to prevent context overflow.
    _MAX = 20_000
    if len(stdout) > _MAX:
        stdout = stdout[:_MAX] + f"\n[…{len(stdout) - _MAX} chars truncated]"

    return ToolOutput(
        success=result.returncode == 0,
        data={
            "command": " ".join(cmd),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
        },
        error=stderr if result.returncode != 0 else None,
    )
