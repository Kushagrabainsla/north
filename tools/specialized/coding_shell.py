"""BashShell - north's `bash` tool, as the shell a coding run uses to test its own changes.

The command is ruled on by the approval layer and runs under the OS sandbox, exactly as any command
north runs for an agent; this only turns the tool's output into what the coding runner needs.
"""

from __future__ import annotations

from coding_agents import ShellResult
from tools.base import Tool
from tools.models import ToolInput

# The exit code `timeout(1)` uses, so a hung test suite reads as a failing one, not a skipped one.
_TIMED_OUT = 124


class BashShell:
    """Runs a command through the registered `bash` tool."""

    def __init__(self, bash: Tool) -> None:
        self._bash = bash

    async def run(self, command: str, workspace: str, *, task_id: str, timeout: int) -> ShellResult:
        output = await self._bash.execute(
            ToolInput(
                params={"command": command, "workspace": workspace, "timeout": timeout, "task_id": task_id},
                granted_workspace=workspace,
            )
        )
        if output.failure_kind == "refused":
            return ShellResult(None, refused=True, error=output.error or "")
        data = output.data or {}
        if "returncode" in data:
            return ShellResult(int(data["returncode"]), f"{data.get('stdout', '')}{data.get('stderr', '')}")
        error = output.error or ""
        return ShellResult(_TIMED_OUT if error.startswith("Command timed out") else None, error, error=error)
