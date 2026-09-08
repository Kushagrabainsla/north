"""BrowserTool — autonomous browser automation and structured web extraction.

Universal tool available to all agents (general, researcher, coder, news_briefing, wellness, etc.).
Wraps the chrome-agent Rust CLI (https://github.com/sderosiaux/chrome-agent) over Chrome CDP.
Provides:
  1. Token-efficient structured record extraction (MDR/DEPTA heuristics via 'extract').
  2. Reader mode article/documentation extraction ('read').
  3. Stable Accessibility Tree (AXTree) element inspection and interaction ('inspect', 'click', 'fill').
  4. Parallel task isolation via `--browser <task_id>`.
  5. Deterministic page assertions with exit codes ('assert').
  6. Multi-tier binary discovery with npx fallback and self-healing diagnostic hints.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from utils.net import UnsafeUrlError, validate_public_url

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_EXTRACT_LIMIT = 25
_DEFAULT_INSPECT_LIMIT = 100
_MAX_OUTPUT_CHARS = 25_000
_MAX_CELL_CHARS = 80
_COLUMN_SAMPLE_SIZE = 5
_ASSERTION_UNMET_EXIT_CODE = 2
_NAVIGATING_ACTIONS = ("goto", "navigate", "read")
_LOOPBACK_HOSTNAMES = ("localhost", "127.0.0.1", "::1")


def _find_chrome_agent_binary() -> list[str] | None:
    """Locate the chrome-agent executable or return an npx fallback command."""
    # 1. Check PATH
    which_bin = shutil.which("chrome-agent")
    if which_bin:
        return [which_bin]

    # 2. Check standard installation locations
    candidates = [
        Path.home() / ".cargo" / "bin" / "chrome-agent",
        Path("/opt/homebrew/bin/chrome-agent"),
        Path("/usr/local/bin/chrome-agent"),
        Path.home() / ".npm-global" / "bin" / "chrome-agent",
        Path.home() / ".local" / "bin" / "chrome-agent",
        Path.home() / ".north" / "bin" / "chrome-agent",
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return [str(p)]

    # 3. Fallback to npx if node/npx is available
    if shutil.which("npx"):
        return ["npx", "-y", "chrome-agent"]

    return None


# How to get chrome-agent, in the order most people can act on. Chrome itself is
# named because the binary is useless without it, and a machine with one and not
# the other reads as "installed but broken" otherwise.
BROWSER_INSTALL_HINT = "cargo install chrome-agent (or install Node for the npx fallback); needs Chrome"


def browser_availability() -> tuple[str, str]:
    """Whether the browser tool can run, as (state, detail) for a status view.

    Three answers, because two of them are not "yes":

    ``available``    a real binary is on this machine
    ``on demand``    only the npx fallback, which downloads on first use - it
                     works, but the first browse pays for it and it needs network
    ``unavailable``  nothing to run; the browser tool will fail every call

    Read by ``north status`` and the System page so the gap is visible before an
    agent discovers it mid-task. Everything else in north works without it.
    """
    command = _find_chrome_agent_binary()
    if command is None:
        return "unavailable", BROWSER_INSTALL_HINT
    if command[0].endswith("npx"):
        return "on demand", "via npx; downloaded on first use"
    return "available", command[0]


def _element_args(params: dict[str, Any]) -> list[str]:
    """Flags naming the element an action applies to, or [] when the caller named none."""
    if params.get("uid"):
        return ["--uid", str(params["uid"])]
    if params.get("selector"):
        return ["--selector", str(params["selector"])]
    return []


def _required_element_args(params: dict[str, Any], action: str) -> list[str]:
    element = _element_args(params)
    if not element:
        raise ValueError(f"Action '{action}' requires 'uid' or 'selector'.")
    return element


def _goto_args(params: dict[str, Any]) -> list[str]:
    url = params.get("url", "").strip()
    if not url:
        raise ValueError("Parameter 'url' is required for action='goto'.")
    args = ["goto", url]
    if params.get("stealth", True):
        args.append("--stealth")
    if params.get("copy_cookies"):
        args.append("--copy-cookies")
    if params.get("connect"):
        args.extend(["--connect", str(params["connect"])])
    if params.get("inspect", True):
        args.append("--inspect")
    return args


def _inspect_args(params: dict[str, Any]) -> list[str]:
    if params.get("diff"):
        return ["diff"]
    limit = params.get("limit", _DEFAULT_INSPECT_LIMIT)
    args = ["inspect", "--limit", str(limit)]
    if params.get("uid"):
        args.extend(["--uid", str(params["uid"])])
    return args


def _extract_args(params: dict[str, Any]) -> list[str]:
    limit = params.get("limit", _DEFAULT_EXTRACT_LIMIT)
    args = ["extract", "--limit", str(limit)]
    if params.get("query"):
        args.extend(["--query", str(params["query"])])
    return args


def _read_args(params: dict[str, Any]) -> list[str]:
    url = params.get("url", "").strip()
    if url:
        return ["goto", url, "--inspect", "read"]
    return ["read"]


def _pointer_args(action: str, params: dict[str, Any]) -> list[str]:
    """A click-like action, which can also target a raw coordinate pair."""
    args = [action]
    if params.get("uid"):
        args.append(str(params["uid"]))
    elif params.get("selector"):
        args.extend(["--selector", str(params["selector"])])
    elif params.get("xy"):
        args.extend(["--xy", str(params["xy"])])
    else:
        raise ValueError(f"Action '{action}' requires 'uid', 'selector', or 'xy'.")
    args.append("--inspect")
    return args


def _click_args(params: dict[str, Any]) -> list[str]:
    return _pointer_args("click", params)


def _dblclick_args(params: dict[str, Any]) -> list[str]:
    return _pointer_args("dblclick", params)


def _fill_args(params: dict[str, Any]) -> list[str]:
    return ["fill", *_required_element_args(params, "fill"), str(params.get("value", "")), "--inspect"]


def _type_args(params: dict[str, Any]) -> list[str]:
    args = ["type", str(params.get("value", ""))]
    if params.get("selector"):
        args.extend(["--selector", str(params["selector"])])
    return args


def _press_args(params: dict[str, Any]) -> list[str]:
    return ["press", str(params.get("value", "Enter"))]


def _select_args(params: dict[str, Any]) -> list[str]:
    args = ["select", *_element_args(params)]
    if "value" in params:
        args.append(str(params["value"]))
    return args


def _check_args(params: dict[str, Any]) -> list[str]:
    return ["check", *_element_args(params)]


def _uncheck_args(params: dict[str, Any]) -> list[str]:
    return ["uncheck", *_element_args(params)]


def _screenshot_args(params: dict[str, Any]) -> list[str]:
    args = ["screenshot"]
    if params.get("filename"):
        args.extend(["--filename", str(params["filename"])])
    return args + _element_args(params)


def _pdf_args(params: dict[str, Any]) -> list[str]:
    args = ["pdf"]
    if params.get("filename"):
        args.extend(["--filename", str(params["filename"])])
    return args


def _eval_args(params: dict[str, Any]) -> list[str]:
    js = params.get("js", "").strip()
    if not js:
        raise ValueError("Parameter 'js' is required for action='eval'.")
    return ["eval", js]


def _assert_args(params: dict[str, Any]) -> list[str]:
    args = ["assert", params.get("assert_type", "text"), *_element_args(params)]
    condition = params.get("assert_condition", "contains")
    if condition:
        args.append(f"--{condition}")
    value = params.get("value", "")
    if value:
        args.append(str(value))
    return args


def _wait_args(params: dict[str, Any]) -> list[str]:
    return ["wait", params.get("condition", "network-idle")]


def _close_args(params: dict[str, Any]) -> list[str]:
    return ["close", "--purge"]


def _status_args(params: dict[str, Any]) -> list[str]:
    return ["status"]


# Every chrome-agent action north exposes, mapped to the builder for its arguments.
# A new action is a new entry here, never another branch in _build_args.
_ACTION_ARGUMENT_BUILDERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "goto": _goto_args,
    "navigate": _goto_args,
    "inspect": _inspect_args,
    "extract": _extract_args,
    "read": _read_args,
    "click": _click_args,
    "dblclick": _dblclick_args,
    "fill": _fill_args,
    "type": _type_args,
    "press": _press_args,
    "select": _select_args,
    "check": _check_args,
    "uncheck": _uncheck_args,
    "screenshot": _screenshot_args,
    "pdf": _pdf_args,
    "eval": _eval_args,
    "assert": _assert_args,
    "wait": _wait_args,
    "close": _close_args,
    "status": _status_args,
}

class _ChromeAgentError(RuntimeError):
    """The chrome-agent process could not be started, or did not finish in time."""


@dataclass(frozen=True)
class _CommandResult:
    """What one chrome-agent invocation left behind."""

    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Payload:
    """The tool-facing reading of a command's stdout."""

    data: dict[str, Any]
    hint: str
    error: str
    ok: bool


def _unsafe_url_reason(action: str, params: dict[str, Any]) -> str:
    """Why this navigation must not happen, or "" when it is safe."""
    url = params.get("url", "")
    if not url or action not in _NAVIGATING_ACTIONS:
        return ""
    # Loopback stays reachable so agents can drive a locally served app under test.
    if urlparse(url).hostname in _LOOPBACK_HOSTNAMES:
        return ""
    try:
        validate_public_url(url)
    except UnsafeUrlError as exc:
        return f"Security policy blocked navigation: {exc}"
    return ""


async def _run_chrome_agent(command: list[str], timeout: int) -> _CommandResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        raise _ChromeAgentError(f"Subprocess execution failed: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill_session(proc)
        raise _ChromeAgentError(f"browser timed out after {timeout}s.") from None
    except Exception as exc:
        raise _ChromeAgentError(f"Subprocess execution failed: {exc}") from exc

    return _CommandResult(
        returncode=proc.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
        stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
    )


async def _kill_session(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group - chrome-agent leaves a browser behind otherwise."""
    with contextlib.suppress(ProcessLookupError, OSError):
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.communicate()


def _parse_payload(action: str, stdout: str) -> _Payload:
    data: dict[str, Any] = {"action": action}
    if not (stdout.startswith("{") and stdout.endswith("}")):
        data["raw_output"] = stdout
        return _Payload(data=data, hint="", error="", ok=True)
    try:
        parsed = json.loads(stdout)
    except Exception:
        data["raw_output"] = stdout
        return _Payload(data=data, hint="", error="", ok=True)
    if not isinstance(parsed, dict):
        return _Payload(data=data, hint="", error="", ok=True)
    data.update(parsed)
    return _Payload(
        data=data,
        hint=parsed.get("hint", ""),
        error=parsed.get("error", ""),
        ok=bool(parsed.get("ok", True)),
    )


def _with_hint(message: str, hint: str) -> str:
    return f"{message} (Hint: {hint})" if hint else message


def _tool_output_for(action: str, result: _CommandResult) -> ToolOutput:
    if action == "assert" and result.returncode == _ASSERTION_UNMET_EXIT_CODE:
        return ToolOutput(
            success=False,
            data={"action": "assert", "held": False, "stdout": result.stdout, "stderr": result.stderr},
            error=f"Assertion unmet: {result.stdout or result.stderr}",
        )

    payload = _parse_payload(action, result.stdout)
    if not payload.ok and result.returncode == 0:
        return ToolOutput(
            success=False,
            data=payload.data,
            error=_with_hint(payload.error or "Unknown browser failure.", payload.hint),
        )
    if result.returncode != 0:
        reported = payload.error or result.stderr or result.stdout
        return ToolOutput(
            success=False,
            data=payload.data,
            error=_with_hint(reported or f"browser exited with status {result.returncode}.", payload.hint),
        )

    if len(result.stdout) > _MAX_OUTPUT_CHARS:
        payload.data["truncated"] = True
    return ToolOutput(success=True, data=payload.data)


def _record_columns(items: list[dict[str, Any]]) -> list[str]:
    """Column order for the table, taken from the first few records."""
    columns: list[str] = []
    for item in items[:_COLUMN_SAMPLE_SIZE]:
        columns.extend(key for key in item if key not in columns)
    return columns


def _table_cell(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    if len(text) > _MAX_CELL_CHARS:
        return text[: _MAX_CELL_CHARS - 3] + "…"
    return text


def _format_records(data: dict[str, Any]) -> str:
    items = data["items"]
    if not items:
        return "No structured records found on page."

    columns = _record_columns(items)
    lines = [
        f"### Extracted {data.get('count', len(items))} Records:",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    lines.extend("| " + " | ".join(_table_cell(item.get(column, "")) for column in columns) + " |" for item in items)
    return "\n".join(lines)


def _format_article(article: dict[str, Any]) -> str:
    title = article.get("title", "Article")
    byline = f" by {article['byline']}" if article.get("byline") else ""
    content = article.get("content", article.get("text", ""))
    return f"## {title}{byline}\n\n{content}"


def _format_assertion(data: dict[str, Any]) -> str:
    outcome = "PASS" if data.get("held", True) else "FAIL"
    return f"Assertion [{outcome}]: {json.dumps(data)}"

class BrowserTool(Tool):
    """Automate Chrome and extract structured web data via Chrome DevTools Protocol."""

    name = "browser"
    is_mutating = False
    description = (
        "Drive a real Chrome browser. Use it ONLY when plain text is not enough: the page "
        "needs a login or a click, it builds itself in JavaScript so fetch_url returns an "
        "empty or skeleton page, you must fill in a form, or you want the rows of a table "
        "or listing as structured records rather than prose. "
        "For an ordinary article, documentation page or any URL you only need to READ, use "
        "fetch_url instead - it is one request against this tool's whole browser, so reach "
        "for a browser only after text has failed or the task needs hands. "
        "Actions:\n"
        "  - 'goto' (or 'navigate'): Navigate to URL. Supports --stealth, --copy-cookies, --connect.\n"
        "  - 'extract': Discover and extract structured lists/tables as JSON records (saves 80% tokens).\n"
        "  - 'read': Reader-mode extraction of clean article/doc text without ads/nav.\n"
        "  - 'inspect': Accessibility Tree (AXTree) with stable numeric UIDs (e.g. n12), or diff=True.\n"
        "  - 'click', 'dblclick': Click element by UID (e.g. uid='n12'), selector, or coordinates.\n"
        "  - 'fill', 'type', 'press': Enter text into input fields or send key presses.\n"
        "  - 'select', 'check', 'uncheck': Manipulate dropdowns and checkboxes.\n"
        "  - 'screenshot', 'pdf': Capture viewport or full page to a file.\n"
        "  - 'eval': Execute JavaScript in the page context.\n"
        "  - 'assert': Deterministically verify page text, values, element existence, or state.\n"
        "  - 'wait': Wait for text, selector, URL, or network-idle.\n"
        "  - 'close': Close the browser instance or tab for this task."
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "goto",
                    "navigate",
                    "inspect",
                    "extract",
                    "read",
                    "click",
                    "dblclick",
                    "fill",
                    "type",
                    "press",
                    "select",
                    "check",
                    "uncheck",
                    "screenshot",
                    "pdf",
                    "eval",
                    "assert",
                    "wait",
                    "close",
                    "status",
                ],
                "description": "Action to perform in the browser.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for goto/navigate/read).",
            },
            "uid": {
                "type": "string",
                "description": "Node UID from inspect (e.g. 'n12', 'n82') to interact with.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector fallback for click, fill, or assert.",
            },
            "value": {
                "type": "string",
                "description": "Text to fill/type, option to select, or expected value for assert.",
            },
            "query": {
                "type": "string",
                "description": "Optional search/filter query for extract.",
            },
            "limit": {
                "type": "integer",
                "description": "Max items to return for extract (default: 25) or inspect (default: 100).",
                "default": 25,
            },
            "diff": {
                "type": "boolean",
                "description": "If true for inspect, returns only what changed since the prior action.",
                "default": False,
            },
            "assert_type": {
                "type": "string",
                "enum": ["value", "text", "state", "exists", "url"],
                "description": "Type of assertion to perform (for action='assert').",
            },
            "assert_condition": {
                "type": "string",
                "enum": [
                    "equals",
                    "contains",
                    "matches",
                    "checked",
                    "unchecked",
                    "selected",
                    "enabled",
                    "disabled",
                    "visible",
                ],
                "description": "Condition to check for assert.",
            },
            "js": {
                "type": "string",
                "description": "JavaScript snippet to evaluate (for action='eval').",
            },
            "filename": {
                "type": "string",
                "description": "Output path for screenshot or pdf.",
            },
            "stealth": {
                "type": "boolean",
                "description": "Enable bot-detection bypass patches on navigation.",
                "default": True,
            },
            "copy_cookies": {
                "type": "boolean",
                "description": "Import cookies from your local Chrome profile for logged-in access.",
                "default": False,
            },
            "connect": {
                "type": "string",
                "description": "Attach to an existing Chrome instance at ws://... or port 9222.",
            },
            "task_id": {
                "type": "string",
                "description": "Task identifier for browser session concurrency isolation.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30).",
                "default": 30,
            },
        },
        "required": ["action"],
    }

    def __init__(self, binary_cmd: list[str] | None = None) -> None:
        self._binary_cmd = binary_cmd

    def _get_cmd(self) -> list[str]:
        if self._binary_cmd:
            return list(self._binary_cmd)
        found = _find_chrome_agent_binary()
        if found:
            return list(found)
        raise RuntimeError(
            "chrome-agent is not installed. "
            "Install it via 'cargo install chrome-agent' or 'npm install -g chrome-agent' "
            "or ensure 'npx' is available in your PATH."
        )

    def _build_args(self, action: str, params: dict[str, Any], task_id: str) -> list[str]:
        build_action_args = _ACTION_ARGUMENT_BUILDERS.get(action)
        if build_action_args is None:
            raise ValueError(f"Unknown action '{action}'.")

        args: list[str] = ["--json"]
        if task_id:
            args.extend(["--browser", task_id])
        args.extend(build_action_args(params))
        return args

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action", "")
        if action == "extract" and "items" in data:
            return _format_records(data)
        if action == "read" and "article" in data:
            return _format_article(data["article"])
        if action == "inspect" and "tree" in data:
            return f"### Accessibility Tree:\n```yaml\n{data['tree']}\n```"
        if action == "assert":
            return _format_assertion(data)
        return json.dumps(data, indent=2)

    async def run(self, input: ToolInput) -> ToolOutput:
        params = input.params or {}
        action = params.get("action", "") or ("goto" if params.get("url") else "")
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        unsafe_url = _unsafe_url_reason(action, params)
        if unsafe_url:
            return ToolOutput(success=False, error=unsafe_url)

        session_id = params.get("task_id", "") or params.get("session_id", "default")
        try:
            command = self._get_cmd() + self._build_args(action, params, session_id)
        except (RuntimeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))

        try:
            result = await _run_chrome_agent(command, int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)))
        except _ChromeAgentError as exc:
            return ToolOutput(success=False, error=str(exc))

        return _tool_output_for(action, result)
