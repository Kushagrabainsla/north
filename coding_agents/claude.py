"""Claude Code as a coding backend: `claude -p` over stream-json, read-only.

Every flag here is there because of something the lab measured (docs/design/coding-agents.md):
a repo's own settings, hooks and MCP servers can run code, so only the user's settings load;
nothing is pre-allowed and nobody can be asked, so anything not read-only is denied; and the
vendor sandbox is on with north's secrets unreadable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from coding_agents.ask import CLAUDE_FETCH_TOOL, CLAUDE_TOOL, SERVER
from coding_agents.base import CodingBackend, EventSink
from coding_agents.constants import (
    HOOK_TIMEOUT_SECONDS,
    MAX_EVENT_TEXT_CHARS,
    MAX_TEXT_CHARS,
    PROBE_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    SECRET_PATHS,
    STREAM_LINE_LIMIT_BYTES,
)
from coding_agents.environment import agent_environment
from coding_agents.exceptions import CodingAgentError
from coding_agents.gate import HOOKED_TOOLS
from coding_agents.models import (
    Availability,
    Denial,
    EventKind,
    FailureKind,
    Mode,
    RunEvent,
    RunOutcome,
    RunSpec,
)
from coding_agents.process import stop, tail

logger = logging.getLogger(__name__)

_HOOK_SCRIPT = Path(__file__).with_name("hook.py")

# `--permission-prompts none` arrived in this release; without it a denied call may wait for an answer.
MINIMUM_VERSION = (2, 1, 259)

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "description")
_SUMMARY_CHARS = 200

_AUTH_STATUSES = frozenset({401, 403})
_RESOURCE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_CONFIG_STATUSES = frozenset({400, 404})
# The vendor names why it is retrying; see `system/api_retry` in Claude Code's headless docs.
_RETRY_FAILURES: dict[str, FailureKind] = {
    "rate_limit": FailureKind.RESOURCE,
    "overloaded": FailureKind.RESOURCE,
    "server_error": FailureKind.RESOURCE,
    "billing_error": FailureKind.RESOURCE,
    "authentication_failed": FailureKind.AUTH,
    "oauth_org_not_allowed": FailureKind.AUTH,
    "account_on_hold": FailureKind.AUTH,
    "cloud_credential_error": FailureKind.AUTH,
    "model_not_found": FailureKind.CONFIG,
    "invalid_request": FailureKind.CONFIG,
}


def _mcp_servers(spec: RunSpec) -> dict[str, Any]:
    """No MCP server at all, unless the run may ask north: then north's own, and only that."""
    if spec.ask is None:
        return {"mcpServers": {}}
    header = {"Authorization": "Bearer ${NORTH_ASK_TOKEN}"}
    return {"mcpServers": {SERVER: {"type": "http", "url": spec.ask.url, "headers": header}}}


class ClaudeBackend(CodingBackend):
    """Runs the user's installed `claude`, signed in as the user."""

    name = "claude"
    provider = "claude_code"

    def __init__(
        self,
        command: str = "claude",
        *,
        protected_paths: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._protected_paths = (*SECRET_PATHS, *protected_paths)
        self._environment = agent_environment(environment)

    async def probe(self) -> Availability:
        if shutil.which(self._command, path=self._environment.get("PATH")) is None:
            return Availability(False, reason=f"`{self._command}` is not installed")
        version_text = await self._capture("--version")
        found = _VERSION.search(version_text or "")
        if found is None:
            return Availability(False, reason=f"`{self._command} --version` did not answer")
        version = tuple(int(part) for part in found.groups())
        label = ".".join(found.groups())
        if version < MINIMUM_VERSION:
            needed = ".".join(str(part) for part in MINIMUM_VERSION)
            return Availability(False, label, f"Claude Code {label} is too old; run `claude update` (needs {needed})")
        if not _logged_in(await self._capture("auth", "status")):
            return Availability(False, label, "Claude Code is not logged in; run `claude auth login`")
        return Availability(True, label)

    async def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        if spec.mode is Mode.EDIT and spec.gate is None:
            raise CodingAgentError("an edit run needs the gate: nothing may change without it")
        process = await asyncio.create_subprocess_exec(
            *self._argv(spec),
            cwd=spec.workspace,
            env=self._process_environment(spec),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so stopping it stops what it started
            limit=STREAM_LINE_LIMIT_BYTES,
        )
        await on_event(RunEvent(EventKind.STARTED, {"pid": process.pid}))
        stream = _Stream(spec.session_id)
        timed_out = False
        exit_code, stderr = -1, ""
        try:
            exit_code, stderr = await asyncio.wait_for(
                self._drive(process, spec, stream, on_event), RUN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            timed_out = True
        finally:
            await stop(process)
        return _outcome(stream, exit_code=exit_code, stderr=stderr, timed_out=timed_out)

    async def _drive(
        self, process: asyncio.subprocess.Process, spec: RunSpec, stream: _Stream, on_event: EventSink
    ) -> tuple[int, str]:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stderr_tail = asyncio.create_task(tail(process.stderr))
        try:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(spec.task.encode())
                await process.stdin.drain()
                process.stdin.close()
            try:
                async for raw in process.stdout:
                    for event in stream.read(raw):
                        await on_event(event)
            except ValueError as exc:  # a line past the limit; what was read so far still counts
                logger.warning("claude stream line exceeded the limit: %s", exc)
            return await process.wait(), await stderr_tail
        finally:
            stderr_tail.cancel()

    def _argv(self, spec: RunSpec) -> list[str]:
        argv = [
            self._command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "plan" if spec.mode is Mode.PLAN else "default",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(_mcp_servers(spec)),
            "--settings",
            json.dumps(self._settings(spec)),
            "--max-turns",
            str(spec.max_turns),
            "--max-budget-usd",
            str(spec.max_budget_usd),
            "--resume" if spec.resume else "--session-id",
            spec.session_id,
        ]
        if spec.ask is not None:
            # Two tools are allowed by name: the door back to north, and the page fetch it does for the agent.
            # Everything else stays default-deny.
            argv += ["--allowedTools", f"{CLAUDE_TOOL},{CLAUDE_FETCH_TOOL}"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.guidance:
            argv += ["--append-system-prompt", spec.guidance]
        return argv

    def _process_environment(self, spec: RunSpec) -> dict[str, str]:
        """What the agent starts with, plus where its hook asks north when it has one."""
        environment = dict(self._environment)
        if spec.gate is not None:
            environment |= {"NORTH_GATE_URL": spec.gate.url, "NORTH_GATE_TOKEN": spec.gate.token}
        if spec.ask is not None:
            environment["NORTH_ASK_TOKEN"] = spec.ask.token  # the MCP header names it; it never sits in argv
        return environment

    def _settings(self, spec: RunSpec) -> dict[str, Any]:
        """The vendor sandbox, strict, and for an edit run the hook that asks north before each action.

        `autoAllowBashIfSandboxed` is off because on, a hook that fails lets a sandboxed command run
        (measured): off, a broken hook means the command is denied.
        """
        settings: dict[str, Any] = {
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": False,
                "filesystem": {"denyRead": list(self._protected_paths)},
                "network": {"strictAllowlist": True, "allowedDomains": []},
            }
        }
        if spec.gate is not None:
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(_HOOK_SCRIPT))}"
            hook = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}
            settings["hooks"] = {"PreToolUse": [{"matcher": HOOKED_TOOLS, "hooks": [hook]}]}
        return settings

    async def _capture(self, *args: str) -> str:
        """What `claude <args>` printed, or nothing if it failed or took too long."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._command,
                *args,
                env=self._environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            return ""
        return out.decode(errors="replace")


class _Stream:
    """What a run has said so far, built one stream-json line at a time."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.last_text = ""
        self.retry_error = ""
        self.result: Mapping[str, Any] | None = None

    def read(self, raw: bytes) -> list[RunEvent]:
        try:
            message = json.loads(raw)
        except ValueError:
            logger.debug("claude printed a line that is not JSON: %r", raw[:120])
            return []
        if not isinstance(message, dict):
            return []
        kind = message.get("type")
        if kind == "result":
            self.result = message
            self.session_id = str(message.get("session_id") or self.session_id)
            return []
        if kind == "system":
            return self._system(message)
        if kind == "assistant":
            return self._assistant(message)
        if kind == "user":
            return _failed_results(message)
        return []

    def _system(self, message: Mapping[str, Any]) -> list[RunEvent]:
        subtype = message.get("subtype")
        if subtype == "init":
            self.session_id = str(message.get("session_id") or self.session_id)
            return [RunEvent(EventKind.INIT, {"session_id": self.session_id, "model": message.get("model")})]
        if subtype == "api_retry":
            self.retry_error = str(message.get("error") or "")
            fields = ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")
            return [RunEvent(EventKind.RETRY, {name: message.get(name) for name in fields})]
        return []

    def _assistant(self, message: Mapping[str, Any]) -> list[RunEvent]:
        events: list[RunEvent] = []
        for block in _blocks(message):
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                self.last_text = str(block["text"])
                events.append(RunEvent(EventKind.TEXT, {"text": self.last_text[:MAX_EVENT_TEXT_CHARS]}))
            elif block.get("type") == "tool_use":
                events.append(
                    RunEvent(EventKind.TOOL_USE, {"tool": block.get("name"), "summary": _summary(block.get("input"))})
                )
        return events


def _blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = (message.get("message") or {}).get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _failed_results(message: Mapping[str, Any]) -> list[RunEvent]:
    return [
        RunEvent(EventKind.TOOL_RESULT, {"is_error": True, "preview": str(block.get("content"))[:_SUMMARY_CHARS]})
        for block in _blocks(message)
        if block.get("type") == "tool_result" and block.get("is_error")
    ]


def _summary(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _SUMMARY_KEYS:
        if tool_input.get(key):
            return str(tool_input[key])[:_SUMMARY_CHARS]
    return ", ".join(sorted(tool_input))[:_SUMMARY_CHARS]


def _outcome(stream: _Stream, *, exit_code: int, stderr: str, timed_out: bool) -> RunOutcome:
    result = stream.result
    if result is None:
        if timed_out:
            return _failed(stream, FailureKind.LIMIT, f"The run did not finish in {RUN_TIMEOUT_SECONDS:.0f} seconds.")
        return _failed(stream, FailureKind.ERROR, stderr.strip() or f"claude exited with code {exit_code}")

    errors = " ".join(str(error) for error in result.get("errors") or [])
    text = result["result"] if isinstance(result.get("result"), str) else stream.last_text
    answer = text[:MAX_TEXT_CHARS]
    cost = float(result.get("total_cost_usd") or 0)
    turns = int(result.get("num_turns") or 0)
    denials = _denials(result)
    tokens_in, tokens_out = _tokens(result)
    if not result.get("is_error") and result.get("subtype") == "success":
        return RunOutcome(
            True, answer, stream.session_id, cost, turns, denials, tokens_in=tokens_in, tokens_out=tokens_out
        )
    failure = _classify(result, errors, stream.retry_error)
    error = (errors or text or str(result.get("subtype")))[:MAX_TEXT_CHARS]
    return RunOutcome(
        False,
        answer,
        stream.session_id,
        cost,
        turns,
        denials,
        failure,
        error,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _classify(result: Mapping[str, Any], errors: str, retry_error: str) -> FailureKind:
    status = result.get("api_error_status")
    if "No conversation found" in errors:
        return FailureKind.SESSION_LOST
    if str(result.get("subtype", "")).startswith("error_max_"):
        return FailureKind.LIMIT
    if status in _AUTH_STATUSES:
        return FailureKind.AUTH
    if status in _RESOURCE_STATUSES:
        return FailureKind.RESOURCE
    if status in _CONFIG_STATUSES:
        return FailureKind.CONFIG
    return _RETRY_FAILURES.get(retry_error, FailureKind.ERROR)


def _failed(stream: _Stream, failure: FailureKind, error: str) -> RunOutcome:
    return RunOutcome(
        ok=False, text=stream.last_text[:MAX_TEXT_CHARS], session_id=stream.session_id, failure=failure, error=error
    )


def _tokens(result: Mapping[str, Any]) -> tuple[int, int]:
    """Prompt and completion tokens, counting what the provider served from its cache as prompt."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt = sum(
        int(usage.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return prompt, int(usage.get("output_tokens") or 0)


def _denials(result: Mapping[str, Any]) -> tuple[Denial, ...]:
    denials = []
    for item in result.get("permission_denials") or []:
        if isinstance(item, dict):
            denials.append(Denial(str(item.get("tool_name") or ""), _summary(item.get("tool_input"))))
    return tuple(denials)


def _logged_in(status_json: str) -> bool:
    try:
        return bool(json.loads(status_json).get("loggedIn"))
    except (ValueError, AttributeError):
        return False
