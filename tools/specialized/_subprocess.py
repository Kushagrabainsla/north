"""Shared synchronous subprocess runner for CLI-wrapping tools.

GitTool and GhTool both shell out to an external binary, capture stdout/stderr,
enforce a timeout, and cap output to a structured ``ToolOutput``. This is the
single definition of that flow (CODING_STYLE §5 DRY). BashTool and ShellTool use
async PTY-based execution instead and deliberately do not share this path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.models import ToolOutput

_DEFAULT_MAX_OUTPUT = 20_000


def run_capture(
    cmd: list[str],
    cwd: Path,
    *,
    timeout: int,
    max_output: int = _DEFAULT_MAX_OUTPUT,
) -> ToolOutput:
    """Run *cmd* in *cwd*, returning a structured ToolOutput.

    Captures stdout/stderr, enforces *timeout* seconds, and truncates stdout to
    *max_output* characters. ``success`` reflects a zero exit code. Intended to be
    called via ``asyncio.to_thread`` from a tool's async ``run()``.
    """
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error=f"Command timed out after {timeout}s.")
    except FileNotFoundError:
        return ToolOutput(success=False, error=f"Executable not found: {cmd[0]}")
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if len(stdout) > max_output:
        stdout = stdout[:max_output] + f"\n[…{len(stdout) - max_output} chars truncated]"

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
