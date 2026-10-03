"""Codex as a coding backend: `codex app-server` over JSON-RPC, in the user's own login.

What the lab measured (docs/design/coding-agents.md): a permissions profile in `thread/start`'s `config`
confines what the agent may read and write (an explicit `sandbox` would override it and the deny-read would
silently stop working, so none is passed); inside the profile an edit in the working directory needs no
approval, and anything the sandbox refuses arrives as an approval request. Plan mode is a read-only profile
and declines every request. Edit mode asks north's gate, over the route the Claude hook uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from coding_agents.appserver import AppServer, AppServerError
from coding_agents.base import CodingBackend, EventSink
from coding_agents.confinement import codex_filesystem, profile
from coding_agents.constants import (
    MAX_EVENT_TEXT_CHARS,
    MAX_TEXT_CHARS,
    PROBE_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    SECRET_PATHS,
    STREAM_LINE_LIMIT_BYTES,
)
from coding_agents.environment import agent_environment
from coding_agents.exceptions import CodingAgentError
from coding_agents.gate import Decision
from coding_agents.gate_client import ask_gate
from coding_agents.models import (
    Availability,
    Denial,
    EventKind,
    FailureKind,
    GateAccess,
    Mode,
    RunEvent,
    RunOutcome,
    RunSpec,
)
from coding_agents.process import stop, tail

logger = logging.getLogger(__name__)

# The newest release the lab measured; the protocol below is checked against the installed one at probe time.
MINIMUM_VERSION = (0, 159, 0)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PROFILE = "northworker"
# Server messages north never reads, switched off so a long run is not a flood.
_NOISE = [
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
    "mcpServer/startupStatus/updated",
    "remoteControl/status/changed",
    "account/updated",
    "thread/status/changed",
    "turn/diff/updated",
]
# What north needs the installed app-server to speak.
_CLIENT_METHODS = ("thread/start", "thread/resume", "turn/start", "turn/interrupt")
_SERVER_REQUESTS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")
_SUMMARY_CHARS = 200

_RESOURCE_INFO = frozenset(
    {
        "usageLimitExceeded",
        "rateLimitExceeded",
        "serverOverloaded",
        "internalServerError",
        "flexUnavailable",
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
    }
)
_LIMIT_INFO = frozenset({"contextWindowExceeded", "sessionBudgetExceeded", "tooManyDenials"})
_AUTH_STATUSES = frozenset({401, 403})
_RESOURCE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


class CodexBackend(CodingBackend):
    """Runs the user's installed `codex`, signed in as the user."""

    name = "codex"
    provider = "codex"

    def __init__(
        self,
        command: str = "codex",
        *,
        protected_paths: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        # The agent needs its own `~/.codex` (its login); it must not read Claude's, north's or anyone's secrets.
        self._protected_paths = (*(path for path in SECRET_PATHS if path != "~/.codex"), "~/.claude", *protected_paths)
        self._environment = agent_environment(environment)

    async def probe(self) -> Availability:
        if shutil.which(self._command, path=self._environment.get("PATH")) is None:
            return Availability(False, reason=f"`{self._command}` is not installed")
        found = _VERSION.search(await self._capture("--version"))
        if found is None:
            return Availability(False, reason=f"`{self._command} --version` did not answer")
        version = tuple(int(part) for part in found.groups())
        label = ".".join(found.groups())
        if version < MINIMUM_VERSION:
            needed = ".".join(str(part) for part in MINIMUM_VERSION)
            return Availability(False, label, f"Codex {label} is too old; update it (needs {needed})")
        if not (await self._capture("login", "status")).strip().lower().startswith("logged in"):  # not "Not logged in"
            return Availability(False, label, "Codex is not logged in; run `codex login`")
        if not await self._speaks_the_protocol():
            return Availability(False, label, "Codex's app-server protocol has changed; north needs an update")
        return Availability(True, label)

    async def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        if spec.mode is Mode.EDIT and spec.gate is None:
            raise CodingAgentError("an edit run needs the gate: nothing may change without it")
        process = await asyncio.create_subprocess_exec(
            self._command,
            "app-server",
            cwd=spec.workspace,
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=STREAM_LINE_LIMIT_BYTES,
        )
        assert process.stderr is not None
        await on_event(RunEvent(EventKind.STARTED, {"pid": process.pid}))
        server = AppServer(process)
        stderr = asyncio.create_task(tail(process.stderr))
        run = _Run(spec.session_id)
        timed_out = False
        try:
            await asyncio.wait_for(self._drive(server, spec, run, on_event), RUN_TIMEOUT_SECONDS)
        except TimeoutError:
            timed_out = True
            await self._interrupt(server, run)
        except AppServerError as exc:
            run.failure_text = run.failure_text or str(exc)
        except asyncio.CancelledError:
            await self._interrupt(server, run)
            raise
        finally:
            await server.close()
            await stop(process)
            run.stderr = await _finished(stderr)
        return run.outcome(timed_out=timed_out)

    async def _drive(self, server: AppServer, spec: RunSpec, run: _Run, on_event: EventSink) -> None:
        await server.request(
            "initialize",
            {"clientInfo": {"name": "north", "version": "0"}, "capabilities": {"optOutNotificationMethods": _NOISE}},
        )
        await server.notify("initialized")
        params = self._thread_params(spec)
        method = "thread/resume" if spec.resume else "thread/start"
        started = await server.request(method, {"threadId": spec.session_id, **params} if spec.resume else params)
        run.session_id = str(started["thread"]["id"])
        await on_event(RunEvent(EventKind.INIT, {"session_id": run.session_id, "model": started.get("model")}))
        turn = await server.request(
            "turn/start", {"threadId": run.session_id, "input": [{"type": "text", "text": spec.task}]}
        )
        run.turn_id = str(turn["turn"]["id"])
        async for message in server.messages():
            if "method" in message and "id" in message:
                await self._approval(server, spec, run, message, on_event)
            elif message.get("method") == "turn/completed":
                run.finish(message["params"]["turn"])
                return
            else:
                for event in run.read(message):
                    await on_event(event)
        run.failure_text = run.failure_text or "the app-server exited before the turn finished"

    def _thread_params(self, spec: RunSpec) -> dict[str, Any]:
        """Thread settings. No `sandbox`: it would override the profile and with it the deny-read."""
        params: dict[str, Any] = {
            "cwd": spec.workspace,
            "approvalPolicy": "on-request",
            "config": self._profile(spec),
        }
        if spec.guidance:
            params["developerInstructions"] = spec.guidance
        if spec.model:
            params["model"] = spec.model
        return params

    def _profile(self, spec: RunSpec) -> dict[str, Any]:
        base = ":read-only" if spec.mode is Mode.PLAN else ":workspace"
        return profile(
            base, _PROFILE, codex_filesystem(spec.workspace, spec.mode, self._protected_paths, self._own_dirs())
        )

    def _own_dirs(self) -> list[str]:
        """Where the installed `codex` lives, so its sandbox helper can still start inside a closed home."""
        found = shutil.which(self._command, path=self._environment.get("PATH"))
        if found is None:
            return []
        return sorted({str(Path(found).parent), str(Path(os.path.realpath(found)).parent)})

    async def _approval(
        self, server: AppServer, spec: RunSpec, run: _Run, message: Mapping[str, Any], on_event: EventSink
    ) -> None:
        """Answer something the agent's sandbox refused it. Plan mode declines; edit mode asks north's gate."""
        method, params = str(message["method"]), message.get("params") or {}
        asked = run.what_is_asked(method, params)
        if asked is None:  # a kind of request north does not handle: the server reads an error as a refusal
            run.denials.append(Denial(method, ""))
            await server.respond(message["id"], error="north does not handle this request")
            return
        allowed = spec.mode is Mode.EDIT and spec.gate is not None and await self._gate_allows(spec, asked)
        if not allowed:
            run.denials.extend(Denial(tool, detail) for tool, detail, _ in asked)
        await server.respond(message["id"], {"decision": "accept" if allowed else "decline"})

    @staticmethod
    async def _gate_allows(spec: RunSpec, asked: list[tuple[str, str, dict[str, Any]]]) -> bool:
        assert spec.gate is not None
        for tool, _, tool_input in asked:
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": tool_input,
                "cwd": spec.workspace,
            }
            if await ask_gate(GateAccess(spec.gate.url, spec.gate.token), payload) is not Decision.ALLOW:
                return False
        return bool(asked)

    @staticmethod
    async def _interrupt(server: AppServer, run: _Run) -> None:
        """Ask the agent to stop the turn it is on, best effort; the process is stopped either way."""
        if run.turn_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    server.request("turn/interrupt", {"threadId": run.session_id, "turnId": run.turn_id}), 3
                )

    async def _speaks_the_protocol(self) -> bool:
        """Whether the installed app-server still has the methods north uses, read from its own schema."""
        with tempfile.TemporaryDirectory(prefix="north-codex-schema-") as directory:
            await self._capture("app-server", "generate-json-schema", "--out", directory)
            client, server = Path(directory, "ClientRequest.json"), Path(directory, "ServerRequest.json")
            try:
                sent, received = client.read_text(), server.read_text()
            except OSError:
                return False
        return all(f'"{name}"' in sent for name in _CLIENT_METHODS) and all(
            f'"{name}"' in received for name in _SERVER_REQUESTS
        )

    async def _capture(self, *args: str) -> str:
        """What `codex <args>` printed, or nothing if it failed or took too long."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._command,
                *args,
                env=self._environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            return ""
        return out.decode(errors="replace")


async def _finished(task: asyncio.Task[str]) -> str:
    """What a stderr reader collected, once its process has been stopped."""
    try:
        return await asyncio.wait_for(task, 1)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
        return ""


class _Run:
    """What one Codex turn has said and done so far, built one server message at a time."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.turn_id = ""
        self.items: dict[str, Mapping[str, Any]] = {}
        self.last_text = ""
        self.final_text = ""
        self.tokens_in = 0
        self.tokens_out = 0
        self.denials: list[Denial] = []
        self.status = ""
        self.error: Mapping[str, Any] | None = None
        self.failure_text = ""
        self.stderr = ""

    def read(self, message: Mapping[str, Any]) -> list[RunEvent]:
        method, params = message.get("method"), message.get("params") or {}
        if method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            self.tokens_in = int(total.get("inputTokens") or 0)
            self.tokens_out = int(total.get("outputTokens") or 0)
        elif method == "error":
            self.error = params.get("error") or {}
            if params.get("willRetry"):
                return [RunEvent(EventKind.RETRY, {"error": _info_name(self.error), "message": _short(self.error)})]
        elif method == "item/started":
            return self._started(params.get("item") or {})
        elif method == "item/completed":
            return self._completed(params.get("item") or {})
        return []

    def _started(self, item: Mapping[str, Any]) -> list[RunEvent]:
        self.items[str(item.get("id"))] = item
        if item.get("type") == "commandExecution":
            return [
                RunEvent(EventKind.TOOL_USE, {"tool": "Bash", "summary": str(item.get("command", ""))[:_SUMMARY_CHARS]})
            ]
        if item.get("type") == "fileChange":
            paths = ", ".join(change.get("path", "") for change in item.get("changes") or [])
            return [RunEvent(EventKind.TOOL_USE, {"tool": "Edit", "summary": paths[:_SUMMARY_CHARS]})]
        return []

    def _completed(self, item: Mapping[str, Any]) -> list[RunEvent]:
        kind, status = item.get("type"), item.get("status")
        if kind == "agentMessage" and str(item.get("text") or "").strip():
            text = str(item["text"])
            if item.get("phase") == "final_answer":
                self.final_text = text
            self.last_text = text
            return [RunEvent(EventKind.TEXT, {"text": text[:MAX_EVENT_TEXT_CHARS]})]
        if kind in ("commandExecution", "fileChange") and status in ("failed", "declined"):
            return [RunEvent(EventKind.TOOL_RESULT, {"is_error": True, "preview": f"{kind} {status}"})]
        return []

    def what_is_asked(self, method: str, params: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]] | None:
        """The (tool, detail, hook-style input) of each thing an approval request asks for; None when unknown."""
        if method == "item/commandExecution/requestApproval":
            command = str(params.get("command") or "")
            return [("Bash", command[:_SUMMARY_CHARS], {"command": command})] if command else None
        if method == "item/fileChange/requestApproval":
            # The request names only the item; the item, announced just before it, names the files.
            changes = (self.items.get(str(params.get("itemId"))) or {}).get("changes") or []
            asked = [("Write", str(c.get("path", "")), {"file_path": str(c.get("path", ""))}) for c in changes]
            return asked or None
        return None

    def finish(self, turn: Mapping[str, Any]) -> None:
        self.status = str(turn.get("status") or "")
        if turn.get("error"):
            self.error = turn["error"]

    def outcome(self, *, timed_out: bool) -> RunOutcome:
        text = (self.final_text or self.last_text)[:MAX_TEXT_CHARS]
        common: dict[str, Any] = {
            "session_id": self.session_id,
            "denials": tuple(self.denials),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }
        if timed_out:
            return RunOutcome(
                False,
                text,
                failure=FailureKind.LIMIT,
                error=f"The run did not finish in {RUN_TIMEOUT_SECONDS:.0f} seconds.",
                **common,
            )
        if self.status == "completed":
            return RunOutcome(True, text, **common)
        if self.status == "interrupted":
            return RunOutcome(False, text, failure=FailureKind.CANCELLED, error="The run was interrupted.", **common)
        message = _short(self.error) if self.error else (self.failure_text or self.stderr.strip() or "the run failed")
        return RunOutcome(False, text, failure=self._classify(message), error=message, **common)

    def _classify(self, message: str) -> FailureKind:
        error = self.error or {}
        info = _info_name(error)
        status = _http_status(error)
        if "not found" in message.lower() and "thread" in message.lower():
            return FailureKind.SESSION_LOST
        if info in _LIMIT_INFO:
            return FailureKind.LIMIT
        if info == "unauthorized" or status in _AUTH_STATUSES:
            return FailureKind.AUTH
        if info in _RESOURCE_INFO or status in _RESOURCE_STATUSES:
            return FailureKind.RESOURCE
        if info == "badRequest" or status in (400, 404):
            return FailureKind.CONFIG
        return FailureKind.ERROR


def _info_name(error: Mapping[str, Any]) -> str:
    """`codexErrorInfo` is a bare name or an object with one key."""
    info = error.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    return next(iter(info), "") if isinstance(info, dict) and info else ""


def _http_status(error: Mapping[str, Any]) -> int | None:
    """The upstream HTTP status: on the error info when Codex forwards it, else inside a nested JSON message."""
    info = error.get("codexErrorInfo")
    if isinstance(info, dict):
        for value in info.values():
            if isinstance(value, dict) and value.get("httpStatusCode"):
                return int(value["httpStatusCode"])
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        status = json.loads(str(error.get("message") or "")).get("status")
        return int(status) if status is not None else None
    return None


def _short(error: Mapping[str, Any] | None) -> str:
    """The error's own message, unwrapped when the provider nested it as JSON."""
    raw = str((error or {}).get("message") or "")
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        nested = json.loads(raw)
        inner = nested.get("error", nested).get("message")
        if inner:
            return str(inner)
    return raw[:MAX_TEXT_CHARS]
