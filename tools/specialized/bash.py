"""BashTool - run shell commands inside the workspace.

Every command is described to the approval layer before the subprocess is
spawned (see `Tool.execute`). The sandbox setting is injected at startup (see
orchestrator/app.py), so this tool is registered manually.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from tools._path import is_sensitive_path
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from tools.specialized import _os_sandbox
from tools.specialized._egress import EgressProxy
from tools.specialized._sandbox import (
    SandboxConfig,
    build_run_argv,
    docker_available,
)
from utils.edit_scope import narrow_workspace
from utils.processes import stop_process_group

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


def _boundary(input: ToolInput) -> str | None:
    """Where a command may write: the folder the server granted, or a subfolder of it the model chose.

    Never the model's `workspace` alone (CODING_STYLE §16.1.1). With nothing
    granted there is no boundary, and nothing but temp and caches is writable.
    """
    return narrow_workspace(input.granted_workspace or "", str(input.params.get("workspace") or "")) or None


def _cwd(input: ToolInput, boundary: str | None) -> str | None:
    """Where the command starts. Not a limit on anything: the sandbox is."""
    return str(input.params.get("workspace") or "") or boundary


def _outside_folders(input: ToolInput) -> tuple[str, ...] | ToolOutput:
    """The folders outside the boundary the command asked to write to, resolved.

    A credential folder is refused here, before any card: no answer makes it writable.
    """
    raw = input.params.get("write_outside") or []
    boundary = _boundary(input)
    root = Path(boundary).resolve() if boundary else None
    folders: list[str] = []
    for item in [raw] if isinstance(raw, str) else raw:
        folder = Path(str(item)).expanduser().resolve()
        if is_sensitive_path(folder):
            return ToolOutput(
                success=False,
                error=f"`{folder}` holds credentials; no command may write there.",
                failure_kind="refused",
            )
        if root is None or not folder.is_relative_to(root):
            folders.append(str(folder))
    return tuple(dict.fromkeys(folders))


def _cap(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    kept = text[:_MAX_OUTPUT_CHARS]
    omitted = len(text) - _MAX_OUTPUT_CHARS
    return kept + f"\n[…{omitted} chars truncated]"


# A command that has to finish within this under the read-only profile to skip the card.
_PROBE_TIMEOUT = 10


class BashTool(Tool):
    """Runs a shell command and returns stdout, stderr, and return code.

    A command that changes something needs approval before it executes - the card
    shows the exact command string so the user sees precisely what will run. With
    the OS sandbox on (macOS), a command is first tried where the kernel refuses
    every write and the network; one that finishes there changed nothing and
    needs no card, and an approved one runs with writes limited to the task's
    folder, plus any folder outside it named in `write_outside` and shown on its
    card (CODING_STYLE §16.1.1).
    """

    name = "bash"
    is_mutating = True
    description = (
        "Run a shell command and return stdout/stderr/returncode."
        " Default timeout 30 s, pass timeout= (max 300) for longer commands."
        " Commands that only read run at once; anything that writes or uses the network needs approval."
        " Approved commands reach only allowed hosts (package registries, code hosts);"
        " a 403 on CONNECT means the host is not allowed."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workspace": {
                "type": "string",
                "description": "Working directory for the command (optional)",
            },
            "write_outside": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Folders outside the task's folder this command must write to, such as ~/Desktop. "
                    "Only when it has to: the user is asked, with these folders shown."
                ),
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
        sandbox: SandboxConfig | None = None,
        os_sandbox: bool = False,
        allowed_domains: tuple[str, ...] | None = None,
    ) -> None:
        self._sandbox = sandbox or SandboxConfig()
        # One sandbox layer only: Docker, when asked for, replaces Seatbelt.
        self._os_sandbox = _os_sandbox.current() if os_sandbox and not self._sandbox.enabled else None
        self._egress = EgressProxy(allowed_domains) if allowed_domains is not None else EgressProxy()

    def _action(
        self, command: str, *, read_only: bool = False, boundary: str | None = None, outside: tuple[str, ...] = ()
    ) -> Action:
        """What this command is, as facts. What that *means* is the policy's call."""
        return Action(
            agent="bash",
            kind=ActionKind.SHELL_COMMAND,
            summary=command,
            command=command,
            # The folders are part of what you approve: the same command writing elsewhere asks again.
            args=" ".join(f"writes={folder}" for folder in outside),
            workspace=boundary or "",
            read_only=read_only,
            mutating=not read_only,
            leaves_sandbox=bool(outside),
            obviously_destructive=any(hint in command for hint in _OBVIOUS_DESTRUCTIVE_HINTS),
        )

    def format_output(self, data: dict[str, Any]) -> str:
        return str(data.get("stdout", data.get("output", ""))).strip()

    async def describe(self, input: ToolInput) -> Request | None:
        # Whether a catastrophic pattern is refused, and in which modes, is the
        # policy's call - this tool only reports that it recognises one.
        command = input.params.get("command")
        outside = _outside_folders(input)
        if not command or isinstance(outside, ToolOutput):
            return None
        boundary = _boundary(input)
        proved = await self._prove_read_only(command, boundary, _cwd(input, boundary))
        also = "\n\nAlso writes to:\n" + "\n".join(f"- `{folder}`" for folder in outside) if outside else ""
        return Request(
            action=self._action(command, read_only=proved is not None, boundary=boundary, outside=outside),
            title="Shell Command - Approval Required",
            message=f"```\n{command}\n```{also}",
            declined="Command cancelled by user.",
            prepared=proved,
        )

    async def _prove_read_only(self, command: str, boundary: str | None, cwd: str | None) -> ToolOutput | None:
        """Run *command* where the kernel refuses every write and the network.

        Returns its output when it finished successfully without being refused
        anything, which proves it changed nothing - the output is then what `run`
        returns, so the command runs once. Returns None when it was refused, failed
        or took too long: it goes to approval, and runs again if approved.

        A command that merely *failed* proves nothing. The sandbox's refusal does
        not always say so: pytest, refused its scratch file, reports "No usable
        temporary directory found", which no marker recognises, and its failed
        probe was then returned as the command's real result without the command
        ever running where it could write.
        """
        if not self._os_sandbox:
            return None
        argv = self._os_sandbox.wrap(command, boundary, writable=False)
        output = await self._spawn(argv, cwd, _PROBE_TIMEOUT, command=command)
        if output.data is None or output.data.get("returncode") != 0:
            return None
        if self._os_sandbox.denied(f"{output.data.get('stderr', '')}\n{output.data.get('stdout', '')}"):
            return None
        return output

    async def _resolve_execution(
        self, command: str, cwd: str | None, boundary: str | None, outside: tuple[str, ...]
    ) -> tuple[list[str] | None, str | None, str | None]:
        """Decide how to run *command*: (argv, host_cwd, error).

        - No sandbox → (None, cwd, None): run on the host as before.
        - Docker on + available + a folder to mount → (docker argv, None, None).
        - Docker on but missing, or no folder → (None, None, error): fail
          closed, because a requested security control must never silently degrade.
        - OS sandbox on → (seatbelt argv, cwd, None): writes limited to *boundary*
          and the approved *outside* folders. With no boundary, only temp and caches:
          it used to fall back to the folder north was started in.
        """
        if self._os_sandbox:
            port = await self._egress.start()
            argv = self._os_sandbox.wrap(command, boundary, writable=True, proxy_port=port, also_writable=outside)
            return argv, cwd, None
        if not self._sandbox.enabled:
            return None, cwd, None
        if not boundary:
            return None, None, "Sandboxed execution requires a workspace to mount, but none was provided."
        if not await docker_available():
            return (
                None,
                None,
                "Sandboxed execution is enabled but Docker is unavailable - refusing to run on the host.",
            )
        return build_run_argv(command, boundary, self._sandbox), None, None

    async def aclose(self) -> None:
        await self._egress.stop()

    async def run(self, input: ToolInput) -> ToolOutput:
        command = input.params.get("command")
        if not command:
            return ToolOutput(success=False, error="Parameter 'command' is required.")
        if isinstance(proved := prepared(input), ToolOutput):
            return proved

        outside = _outside_folders(input)
        if isinstance(outside, ToolOutput):
            return outside
        boundary = _boundary(input)
        cwd = _cwd(input, boundary)
        raw_timeout = input.params.get("timeout")
        try:
            timeout = min(max(int(raw_timeout), 1), 300) if raw_timeout is not None else _TIMEOUT
        except (ValueError, TypeError):
            timeout = _TIMEOUT

        exec_argv, exec_cwd, sandbox_error = await self._resolve_execution(command, cwd, boundary, outside)
        if sandbox_error is not None:
            return ToolOutput(success=False, error=sandbox_error)
        output = await self._spawn(exec_argv, exec_cwd, timeout, command=command)
        if not output.success and self._os_sandbox and self._os_sandbox.denied(output.error or ""):
            # Refused, not broken: say how to ask, so the work is not dropped.
            where = f"in `{boundary}`" if boundary else "only in temp folders"
            output.error = (
                f"{output.error}\nThe sandbox lets this command write {where}. If it must write somewhere else, "
                "call bash again with write_outside=[that folder]; the user is asked first."
            )
        return output

    async def _spawn(self, argv: list[str] | None, cwd: str | None, timeout: int, *, command: str) -> ToolOutput:
        """Run *argv* - or *command* through the host shell when there is none - and collect its output."""
        try:
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    start_new_session=True,
                )
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            return ToolOutput(success=False, error=f"Command timed out after {timeout}s.")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        finally:
            # A timeout or a cancelled task stops the command and whatever it started.
            await stop_process_group(proc, grace_seconds=1.0)

        stdout = _cap(stdout_b.decode("utf-8", errors="replace"))
        stderr = _cap(stderr_b.decode("utf-8", errors="replace"))
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
