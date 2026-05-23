"""BashTool — run shell commands inside the workspace.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_TIMEOUT = 30

_BLOCKED = [
    "rm -rf /",
    ":(){ :|:& };:",
    "dd if=",
    "> /dev/sd",
]


class BashTool(Tool):
    """Runs a shell command and returns stdout, stderr, and return code."""

    name = "bash"
    description = (
        "Run a shell command (30s timeout). "
        "Params: command (str), workspace (str, optional, used as cwd)."
    )

    async def run(self, input: ToolInput) -> ToolOutput:
        command = input.params.get("command")
        if not command:
            return ToolOutput(success=False, error="Parameter 'command' is required.")

        for blocked in _BLOCKED:
            if blocked in command:
                return ToolOutput(success=False, error=f"Blocked pattern in command: {blocked!r}")

        cwd = input.params.get("workspace") or None

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolOutput(success=False, error=f"Command timed out after {_TIMEOUT}s.")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

        return ToolOutput(
            success=proc.returncode == 0,
            data={
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
                "returncode": proc.returncode,
            },
        )
