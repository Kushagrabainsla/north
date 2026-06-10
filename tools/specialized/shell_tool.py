"""ShellTool — long-lived PTY shell sessions for watch processes, servers, REPLs.

Unlike BashTool (one-shot: run, capture, exit), ShellTool keeps a process alive
behind a pseudo-terminal so an agent can start `npm run dev` / `tsc --watch` /
a debugger or REPL, then read streamed output, send input, and stop it across
several tool calls.

Each session is PTY-backed (stdlib `pty`, no third-party dependency) so child
processes see a real terminal (`isatty()` true) and behave as they would
interactively. Output is buffered as it arrives via the event loop's reader.

Safety mirrors BashTool: `start` and `write` are gated behind the same approval
flow (the user sees the exact command / input). `read`, `stop`, and `list`
operate on an already-approved session and run immediately.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import signal
import time
from typing import TYPE_CHECKING, Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import request_approval_decision

if TYPE_CHECKING:
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

# Most output the model needs after one read; a watcher can produce far more, so
# the per-session buffer keeps only the most recent slice.
_MAX_BUFFER_BYTES = 200_000
_READ_CHUNK = 65_536
# Grace period after start/write before the first read, so initial output lands.
_DEFAULT_GRACE_SECONDS = 0.6
_MAX_GRACE_SECONDS = 30.0
# Cap concurrent sessions so a runaway agent can't fork-bomb via long-lived shells.
_MAX_SESSIONS = 8

_ACTIONS = ("start", "read", "write", "stop", "list")


class _ShellSession:
    """One PTY-backed long-lived process and its rolling output buffer."""

    def __init__(self, shell_id: str, command: str, proc: asyncio.subprocess.Process, master_fd: int) -> None:
        self.shell_id = shell_id
        self.command = command
        self.started_at = time.monotonic()
        self._proc = proc
        self._master_fd = master_fd
        self._buffer = bytearray()
        self._read_pos = 0
        self._closed = False
        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._detach_reader()  # EIO: child closed the pty
            return
        if not data:
            self._detach_reader()  # EOF
            return
        self._buffer.extend(data)
        if len(self._buffer) > _MAX_BUFFER_BYTES:
            drop = len(self._buffer) - _MAX_BUFFER_BYTES
            del self._buffer[:drop]
            self._read_pos = max(0, self._read_pos - drop)

    def _detach_reader(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(ValueError, OSError):
            asyncio.get_running_loop().remove_reader(self._master_fd)

    @property
    def is_running(self) -> bool:
        return self._proc.returncode is None

    def read_new(self) -> str:
        chunk = self._buffer[self._read_pos :]
        self._read_pos = len(self._buffer)
        return chunk.decode("utf-8", errors="replace")

    def has_pending_output(self) -> bool:
        """True if unread output remains. Non-destructive (does not advance the cursor)."""
        return self._read_pos < len(self._buffer)

    def write(self, text: str) -> None:
        os.write(self._master_fd, text.encode("utf-8"))

    def close(self) -> None:
        """Detach the reader and release the pty master fd (idempotent)."""
        self._detach_reader()
        with contextlib.suppress(OSError):
            os.close(self._master_fd)

    async def stop(self) -> str:
        self._detach_reader()
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                await self._proc.wait()
        with contextlib.suppress(OSError):
            os.read(self._master_fd, _READ_CHUNK)  # drain any final bytes
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        return self.read_new()


class ShellTool(Tool):
    """Start, read, write, and stop long-lived PTY shell sessions."""

    name = "shell"
    is_mutating = True
    description = (
        "Manage long-lived shell sessions for processes that must stay alive across "
        "tool calls (dev servers, file watchers, REPLs, debuggers). "
        "actions: 'start' (launch a command, returns shell_id + initial output; needs approval), "
        "'read' (drain new output for shell_id; pass wait= seconds to block for output), "
        "'write' (send input to shell_id, e.g. a REPL line; needs approval), "
        "'stop' (terminate shell_id, returns final output), 'list' (active sessions). "
        "Use the one-shot 'bash' tool instead for commands that run and exit."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS), "description": "Operation to perform"},
            "command": {"type": "string", "description": "Command to launch (action=start)"},
            "shell_id": {"type": "string", "description": "Session id (action=read/write/stop)"},
            "input": {"type": "string", "description": "Text to send to the session (action=write)"},
            "wait": {
                "type": "number",
                "description": "Seconds to block waiting for output (default 0.6, max 30)",
            },
            "workspace": {"type": "string", "description": "Working directory (action=start)"},
        },
        "required": ["action"],
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
        self._sessions: dict[str, _ShellSession] = {}

    def format_output(self, data: dict[str, Any]) -> str:
        if "sessions" in data:
            sessions = data["sessions"]
            if not sessions:
                return "No active shell sessions."
            return "\n".join(
                f"{s['shell_id']}: {'running' if s['running'] else 'exited'} — {s['command']}" for s in sessions
            )
        header = data.get("shell_id", "")
        output = str(data.get("output", "")).strip()
        running = data.get("running")
        status = "" if running is None else (" [running]" if running else " [exited]")
        return f"[{header}{status}]\n{output}" if header else output

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action", "")).strip()
        if action not in _ACTIONS:
            return ToolOutput(success=False, error=f"action must be one of {list(_ACTIONS)}.")

        handler = {
            "start": self._start,
            "read": self._read,
            "write": self._write,
            "stop": self._stop,
            "list": self._list,
        }[action]
        return await handler(input.params)

    async def _start(self, params: dict[str, Any]) -> ToolOutput:
        command = params.get("command")
        if not command:
            return ToolOutput(success=False, error="action=start requires 'command'.")

        self._reap_exited()
        if len(self._sessions) >= _MAX_SESSIONS:
            return ToolOutput(
                success=False,
                error=f"Too many active sessions ({_MAX_SESSIONS}). Stop one before starting another.",
            )

        if not await self._request_approval(params.get("task_id"), f"start shell:\n{command}"):
            return ToolOutput(success=False, error="Shell start cancelled by user.")

        cwd = params.get("workspace") or None
        try:
            session = await _spawn_session(command, cwd)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to start shell: {exc}")

        self._sessions[session.shell_id] = session
        await asyncio.sleep(_grace(params.get("wait")))
        return ToolOutput(
            success=True,
            data={"shell_id": session.shell_id, "output": session.read_new(), "running": session.is_running},
        )

    async def _read(self, params: dict[str, Any]) -> ToolOutput:
        session = self._sessions.get(str(params.get("shell_id", "")))
        if session is None:
            return ToolOutput(success=False, error="Unknown shell_id. Use action=list to see sessions.")
        await asyncio.sleep(_grace(params.get("wait")))
        return ToolOutput(
            success=True,
            data={"shell_id": session.shell_id, "output": session.read_new(), "running": session.is_running},
        )

    async def _write(self, params: dict[str, Any]) -> ToolOutput:
        session = self._sessions.get(str(params.get("shell_id", "")))
        if session is None:
            return ToolOutput(success=False, error="Unknown shell_id. Use action=list to see sessions.")
        if not session.is_running:
            return ToolOutput(success=False, error="Session has exited; start a new one.")
        text = params.get("input")
        if text is None:
            return ToolOutput(success=False, error="action=write requires 'input'.")

        if not await self._request_approval(params.get("task_id"), f"send to shell {session.shell_id}:\n{text}"):
            return ToolOutput(success=False, error="Shell input cancelled by user.")

        try:
            session.write(text if text.endswith("\n") else text + "\n")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Write failed: {exc}")
        await asyncio.sleep(_grace(params.get("wait")))
        return ToolOutput(
            success=True,
            data={"shell_id": session.shell_id, "output": session.read_new(), "running": session.is_running},
        )

    async def _stop(self, params: dict[str, Any]) -> ToolOutput:
        session = self._sessions.pop(str(params.get("shell_id", "")), None)
        if session is None:
            return ToolOutput(success=False, error="Unknown shell_id. Use action=list to see sessions.")
        output = await session.stop()
        return ToolOutput(success=True, data={"shell_id": session.shell_id, "output": output, "running": False})

    async def _list(self, params: dict[str, Any]) -> ToolOutput:
        self._reap_exited()
        sessions = [
            {
                "shell_id": s.shell_id,
                "command": s.command,
                "running": s.is_running,
                "age_seconds": round(time.monotonic() - s.started_at, 1),
            }
            for s in self._sessions.values()
        ]
        return ToolOutput(success=True, data={"sessions": sessions})

    def _reap_exited(self) -> None:
        """Drop exited sessions whose output has already been drained.

        Uses a non-destructive pending check so reaping never consumes output the
        agent has not read yet (a `list` or `start` must not destroy a finished
        process's buffered output).
        """
        for shell_id in list(self._sessions):
            session = self._sessions[shell_id]
            if not session.is_running and not session.has_pending_output():
                session.close()
                self._sessions.pop(shell_id, None)

    async def _request_approval(self, task_id: str | None, message: str) -> bool:
        """Gate a command/input behind the shared tool approval flow."""
        return await request_approval_decision(
            self._approval_store,
            task_id=task_id,
            agent="shell",
            title="Shell Session — Approval Required",
            message=f"```\n{message}\n```",
            stream_manager=self._stream_manager,
            judgement_filter=self._judgement_filter,
            timeout=self._approval_timeout_seconds,
        )


def _grace(raw: Any) -> float:
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_GRACE_SECONDS
    return min(max(wait, 0.0), _MAX_GRACE_SECONDS)


async def _spawn_session(command: str, cwd: str | None) -> _ShellSession:
    from utils.ids import generate_id

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)  # parent keeps only the master end
    return _ShellSession(generate_id(), command, proc, master_fd)
