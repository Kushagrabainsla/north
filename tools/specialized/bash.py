"""BashTool — run shell commands inside the workspace.

Every command is gated behind an explicit user approval card before the
subprocess is spawned. The ApprovalStore + EventStreamManager are injected
at startup (see orchestrator/app.py), so this tool must be registered
manually rather than auto-discovered.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import request_approval_decision

if TYPE_CHECKING:
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

_TIMEOUT = 30
# Stdout/stderr are capped so a single `cat` of a large file can't overflow the
# model's context window.  The tail is truncated with a visible marker.
_MAX_OUTPUT_CHARS = 30_000

# These patterns are caught as a UX convenience — NOT a security boundary.
# The approval gate above is the actual guard: the user sees the exact command
# and decides. This list only catches the most obviously destructive typos.
# Do not rely on it to stop a determined attacker or a misbehaving model —
# any determined bypass (extra spaces, equivalent syntax) will get through.
_OBVIOUS_DESTRUCTIVE_HINTS = [
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


class CommandSafetyInspector:
    """Inspector responsible for checking if a command is instantly safe/read-only."""

    def __init__(self) -> None:
        self.instant_safe_prefixes = [
            "git status",
            "git diff",
            "git log",
            "git show",
            "git branch",
            "cat ",
            "grep ",
            "find ",
            "ls ",
            "pwd",
            "whoami",
        ]

    def is_instantly_safe(self, command: str) -> bool:
        cleaned = command.strip().lower()
        return any(cleaned.startswith(prefix) for prefix in self.instant_safe_prefixes)


class BashTool(Tool):
    """Runs a shell command and returns stdout, stderr, and return code.

    Requires explicit user approval before executing any command — the approval
    card shows the exact command string so the user sees precisely what will run.
    """

    name = "bash"
    is_mutating = True
    description = (
        "Run a shell command and return stdout/stderr/returncode."
        " Default timeout 30 s, pass timeout= (max 300) for longer commands."
        " Every command requires user approval before it executes."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workspace": {
                "type": "string",
                "description": "Working directory for the command (optional)",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Timeout in seconds (default 30, max 300). "
                    "Use higher values for test suites or long-running builds."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        approval_store: ApprovalStore,
        stream_manager: EventStreamManager | None = None,
        approval_timeout_seconds: float = 300.0,
        judgement_filter: JudgementFilter | None = None,
    ) -> None:
        self._approval_store = approval_store
        self._stream_manager = stream_manager
        self._approval_timeout_seconds = approval_timeout_seconds
        self._judgement_filter = judgement_filter
        self._safety_inspector = CommandSafetyInspector()

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("stdout", data.get("output", ""))).strip()

    async def _request_approval(self, task_id: str | None, command: str) -> bool:
        """Emit an approval card for the command. Returns True if the user approves."""
        if self._safety_inspector.is_instantly_safe(command):
            return True

        return await request_approval_decision(
            self._approval_store,
            task_id=task_id,
            agent="bash",
            title="Shell Command — Approval Required",
            message=f"```\n{command}\n```",
            stream_manager=self._stream_manager,
            judgement_filter=self._judgement_filter,
            timeout=self._approval_timeout_seconds,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        command = input.params.get("command")
        if not command:
            return ToolOutput(success=False, error="Parameter 'command' is required.")

        for blocked in _OBVIOUS_DESTRUCTIVE_HINTS:
            if blocked in command:
                return ToolOutput(success=False, error=f"Blocked pattern in command: {blocked!r}")

        task_id: str | None = input.params.get("task_id")
        approved = await self._request_approval(task_id, command)
        if not approved:
            return ToolOutput(success=False, error="Command cancelled by user.")

        cwd = input.params.get("workspace") or None
        raw_timeout = input.params.get("timeout")
        try:
            timeout = min(max(int(raw_timeout), 1), 300) if raw_timeout is not None else _TIMEOUT
        except (ValueError, TypeError):
            timeout = _TIMEOUT

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolOutput(success=False, error=f"Command timed out after {timeout}s.")
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
