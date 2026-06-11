"""BashTool — run shell commands inside the workspace.

Every command is gated behind an explicit user approval card before the
subprocess is spawned. The ApprovalStore + EventStreamManager are injected
at startup (see orchestrator/app.py), so this tool must be registered
manually rather than auto-discovered.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from tools._path import references_sensitive_path
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


# Any of these makes a command compound — chaining, substitution, redirection,
# subshells, or find's `{}` placeholder. A safe-looking prefix then proves
# nothing about what actually runs, so the command always goes to approval.
_SHELL_METACHARS = re.compile(r"[;&|`$<>(){}\n]")
# find actions that mutate the filesystem or write files; `find` is otherwise
# read-only and stays on the fast path.
_FIND_MUTATING_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")


class CommandSafetyInspector:
    """Inspector responsible for checking if a command is instantly safe/read-only.

    The prefix list is a convenience fast-path, not a security boundary — but it
    must not be trivially escapable. Compound commands (shell metacharacters),
    mutating `find` actions, and reads of sensitive paths (~/.ssh, /etc, ...)
    all fall through to the approval card.
    """

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
        cleaned = command.strip()
        if _SHELL_METACHARS.search(cleaned):
            return False
        lowered = cleaned.lower()
        if not any(lowered.startswith(prefix) for prefix in self.instant_safe_prefixes):
            return False
        if lowered.startswith("find ") and any(flag in lowered for flag in _FIND_MUTATING_FLAGS):
            return False
        return not references_sensitive_path(cleaned)


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
