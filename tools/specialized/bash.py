"""BashTool — run shell commands inside the workspace.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_TIMEOUT = 30
# Stdout/stderr are capped so a single `cat` of a large file can't overflow the
# model's context window.  The tail is truncated with a visible marker.
_MAX_OUTPUT_CHARS = 30_000

_BLOCKED = [
    "rm -rf /",
    ":(){ :|:& };:",
    "dd if=",
    "> /dev/sd",
]


def _cap(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    kept = text[:_MAX_OUTPUT_CHARS]
    omitted = len(text) - _MAX_OUTPUT_CHARS
    return kept + f"\n[…{omitted} chars truncated]"


class BashTool(Tool):
    """Runs a shell command and returns stdout, stderr, and return code."""

    name = "bash"
    description = "Run a shell command (30 s timeout) and return stdout/stderr/returncode."
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workspace": {
                "type": "string",
                "description": "Working directory for the command (optional)",
            },
        },
        "required": ["command"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("stdout", data.get("output", ""))).strip()

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
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolOutput(success=False, error=f"Command timed out after {_TIMEOUT}s.")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        stdout = _cap(stdout)
        stderr = _cap(stderr)
        success = proc.returncode == 0
        return ToolOutput(
            success=success,
            error=None if success else (stderr.strip() or f"exit code {proc.returncode}"),
            data={
                "stdout": stdout,
                "stderr": stderr,
                "returncode": proc.returncode,
            },
        )
