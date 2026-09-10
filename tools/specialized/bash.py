"""BashTool - run shell commands inside the workspace.

Every command is gated behind an explicit user approval card before the
subprocess is spawned. The ApprovalStore + event emitter are injected
at startup (see orchestrator/app.py), so this tool must be registered
manually rather than auto-discovered.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from typing import TYPE_CHECKING, Any

from approval.policy import Action, ActionKind
from tools._path import references_sensitive_path
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_action
from tools.specialized._sandbox import (
    SandboxConfig,
    build_run_argv,
    docker_available,
)

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import ApprovalPolicy
    from approval.store import ApprovalStore
    from utils.events import EventEmitter

_TIMEOUT = 30
# Stdout/stderr are capped so a single `cat` of a large file can't overflow the
# model's context window.  The tail is truncated with a visible marker.
_MAX_OUTPUT_CHARS = 30_000

# These patterns are caught as a UX convenience - NOT a security boundary.
# The approval gate above is the actual guard: the user sees the exact command
# and decides. This list only catches the most obviously destructive typos.
# Do not rely on it to stop a determined attacker or a misbehaving model  -
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


# Any of these makes a command compound - chaining, substitution, redirection,
# subshells, or placeholders. A safe-looking prefix then proves nothing about
# what actually runs, so the command always goes to approval.
_SHELL_METACHARS = re.compile(r"[;&|`$<>(){}\n]")


class CommandSafetyInspector:
    """Inspector responsible for checking if a command is instantly safe/read-only.

    The prefix list is a convenience fast-path, not a security boundary - but it
    must not be trivially escapable. Compound commands (shell metacharacters),
    filesystem-traversal commands (`find` - it can walk arbitrary trees and run
    actions), and reads of sensitive paths (~/.ssh, ~/.north, /etc, ...) all
    fall through to the approval card.
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
        if lowered.startswith("grep ") and _grep_is_recursive(cleaned):
            return False
        return not references_sensitive_path(cleaned)


def _grep_is_recursive(command: str) -> bool:
    """True when a grep command walks a directory tree (-r/-R/--recursive)."""
    for token in command.split()[1:]:
        if token in ("--recursive", "--dereference-recursive"):
            return True
        if token.startswith("-") and not token.startswith("--") and ("r" in token or "R" in token):
            return True
    return False


class BashTool(ApprovalGatedTool):
    """Runs a shell command and returns stdout, stderr, and return code.

    Requires explicit user approval before executing any command - the approval
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
        stream_manager: EventEmitter | None = None,
        approval_timeout_seconds: float = 300.0,
        policy: ApprovalPolicy | None = None,
        notifier: Notifier | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, policy, notifier)
        self._safety_inspector = CommandSafetyInspector()
        self._sandbox = sandbox or SandboxConfig()

    def _describe(self, command: str) -> Action:
        """What this command is, as facts. What that *means* is the policy's call."""
        return Action(
            agent="bash",
            kind=ActionKind.SHELL_COMMAND,
            summary=command,
            command=command,
            read_only=self._safety_inspector.is_instantly_safe(command),
            mutating=not self._safety_inspector.is_instantly_safe(command),
            obviously_destructive=any(hint in command for hint in _OBVIOUS_DESTRUCTIVE_HINTS),
        )

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("stdout", data.get("output", ""))).strip()

    async def _gate(self, task_id: str | None, command: str) -> ToolOutput | None:
        """``None`` when the command may run; otherwise what to return instead."""
        return await gate_action(
            self._describe(command),
            policy=self._policy,
            approval_store=self._approval_store,
            title="Shell Command - Approval Required",
            message=f"```\n{command}\n```",
            task_id=task_id,
            stream_manager=self._stream_manager,
            notifier=self._notifier,
            timeout=self._approval_timeout_seconds,
            declined="Command cancelled by user.",
        )

    async def _resolve_execution(
        self, command: str, cwd: str | None
    ) -> tuple[list[str] | None, str | None, str | None]:
        """Decide how to run *command*: (docker_argv, host_cwd, error).

        - Sandbox off → (None, cwd, None): run on the host as before.
        - Sandbox on + Docker available + a workspace to mount → (argv, None, None).
        - Sandbox on but Docker missing or no workspace → (None, None, error): fail
          closed, because a requested security control must never silently degrade.
        """
        if not self._sandbox.enabled:
            return None, cwd, None
        if not cwd:
            return None, None, "Sandboxed execution requires a workspace to mount, but none was provided."
        if not await docker_available():
            return (
                None,
                None,
                "Sandboxed execution is enabled but Docker is unavailable - refusing to run on the host.",
            )
        return build_run_argv(command, cwd, self._sandbox), None, None

    async def run(self, input: ToolInput) -> ToolOutput:
        command = input.params.get("command")
        if not command:
            return ToolOutput(success=False, error="Parameter 'command' is required.")

        # Whether a catastrophic pattern is refused, and in which modes, is the
        # policy's call - this tool only reports that it recognises one.
        refused = await self._gate(input.params.get("task_id"), command)
        if refused is not None:
            return refused

        cwd = input.params.get("workspace") or None
        raw_timeout = input.params.get("timeout")
        try:
            timeout = min(max(int(raw_timeout), 1), 300) if raw_timeout is not None else _TIMEOUT
        except (ValueError, TypeError):
            timeout = _TIMEOUT

        exec_argv, exec_cwd, sandbox_error = await self._resolve_execution(command, cwd)
        if sandbox_error is not None:
            return ToolOutput(success=False, error=sandbox_error)

        use_new_session = hasattr(os, "setsid")
        try:
            if exec_argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *exec_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=use_new_session,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=exec_cwd,
                    start_new_session=use_new_session,
                )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                if use_new_session and hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    with contextlib.suppress(ProcessLookupError):
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGTERM)
                        try:
                            await asyncio.wait_for(proc.communicate(), timeout=1.0)
                        except TimeoutError:
                            os.killpg(pgid, signal.SIGKILL)
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(proc.communicate(), timeout=1.0)
                else:
                    proc.kill()
                    with contextlib.suppress(Exception):
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
