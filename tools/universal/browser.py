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
import hashlib
import json
import logging
import os
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config.browser_connection import active_cdp_endpoint
from config.browser_connection import cdp_http_base as _cdp_http_base
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.browser_describe import describe_browser_call
from utils.net import UnsafeUrlError, validate_public_url

if TYPE_CHECKING:
    from approval.approvals import Request as ApprovalRequest
    from config.strategy import NorthSettings

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
_MAX_CDP_RESPONSE_BYTES = 1_000_000


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
    condition = params.get("condition", "network-idle")
    if condition not in {"text", "url", "selector", "network-idle"}:
        raise ValueError("Unknown browser wait condition.")
    args = ["wait", condition]
    if condition != "network-idle":
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("Text, URL and selector waits require pattern.")
        args.append(pattern)
    return args


def _close_args(params: dict[str, Any]) -> list[str]:
    return ["close"]


def _status_args(params: dict[str, Any]) -> list[str]:
    return ["status"]


def _read_cdp_json(url: str, timeout: int) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "north-cdp-preflight"})
    with urlopen(request, timeout=max(1, min(timeout, 10))) as response:  # noqa: S310 - loopback-only above
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        payload = response.read(_MAX_CDP_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_CDP_RESPONSE_BYTES:
        raise ValueError("response was unexpectedly large")
    return json.loads(payload.decode("utf-8"))


def _probe_cdp_endpoint(connect: str, timeout: int) -> ToolOutput:
    """Prove that *connect* is a live Chrome DevTools endpoint, not merely an open port."""
    try:
        base = _cdp_http_base(connect)
        version = _read_cdp_json(f"{base}/json/version", timeout)
        if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
            raise ValueError("/json/version did not return a DevTools WebSocket URL")
        target_count: int | None = None
        try:
            targets = _read_cdp_json(f"{base}/json/list", timeout)
            if isinstance(targets, list):
                target_count = len(targets)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
            # /json/version is the authoritative handshake. Target enumeration
            # is useful context, but not required by every Chrome derivative.
            pass
        return ToolOutput(
            success=True,
            data={
                "action": "preflight",
                "verified": True,
                "endpoint": base,
                "browser": str(version.get("Browser") or "Chrome"),
                "protocol_version": str(version.get("Protocol-Version") or "unknown"),
                "connection_id": hashlib.sha256(str(version["webSocketDebuggerUrl"]).encode()).hexdigest(),
                "target_count": target_count,
            },
        )
    except HTTPError as exc:
        return ToolOutput(
            success=False,
            error=(
                f"CDP preflight failed: {connect} responded with HTTP {exc.code}, but it is not a usable "
                "Chrome DevTools endpoint. Start Chrome with remote debugging and verify /json/version."
            ),
        )
    except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return ToolOutput(success=False, error=f"CDP preflight failed for {connect}: {exc}")


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
    _MUTATING_ACTIONS = frozenset(
        {
            "click",
            "dblclick",
            "fill",
            "type",
            "press",
            "select",
            "check",
            "uncheck",
            "eval",
        }
    )
    description = (
        "Drive a real Chrome browser. Use it ONLY when plain text is not enough: the page "
        "needs a login or a click, it builds itself in JavaScript so fetch_url returns an "
        "empty or skeleton page, you must fill in a form, or you want the rows of a table "
        "or listing as structured records rather than prose. "
        "For an ordinary article, documentation page or any URL you only need to READ, use "
        "fetch_url instead - it is one request against this tool's whole browser, so reach "
        "for a browser only after text has failed or the task needs hands. "
        "Call list_profiles to see saved names and purposes. Select profile_id for each tool call from "
        "the current task context and configured purposes; profiles belong to tool calls, not flows. "
        "If the choice is missing or ambiguous, ask which browser profile to use (for example Personal, "
        "University, Work, or an isolated North browser), unless the user already explicitly chose for "
        "this task. Choosing 'existing browser' alone does not identify a profile. Explain that "
        "existing-browser access can expose logged-in sessions, cookies, open tabs, and extensions; "
        "route questions through ask_user and actions through the ordinary approval layer; the mode "
        "decides who answers. Resolve the chosen context through the supported connection and preflight: "
        "a profile name alone is not a verified connection. Never silently switch profiles or copy "
        "cookies as a fallback. Check the expected account on the target site; if already signed in, "
        "reuse that session without asking the user to log in again. Saved passwords alone do not "
        "prove an active login. Ask for user sign-in help only when the site requires it, such as "
        "an expired session, MFA, or password-manager unlock. Do not read or export passwords or "
        "request them in chat. If connection setup fails, report that blocker rather than presenting "
        "it as a login problem. "
        "When the person must log in, complete MFA, unlock, or take over, stop browser interaction and "
        "call ask_user with requires_user_action=true as its own call; explain the exact help needed "
        "and offer 'Done'. The same mode policy answers this request; an answer alone is not proof of "
        "login. After the answer, then inspect and assert the expected account "
        "or state before continuing the original task in the same profile. Do not end the task as "
        "completed or failed merely because it is waiting, and do not repeat completed external actions. "
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
        "  - 'preflight': Verify an existing-browser CDP endpoint with /json/version before use.\n"
        "  - 'wait': Wait for text, selector, URL, or network-idle.\n"
        "  - 'close': Close the browser instance or tab for this task."
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_profiles",
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
                    "preflight",
                ],
                "description": "Action to perform in the browser.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for goto/navigate/read).",
            },
            "profile_id": {
                "type": "string",
                "description": "Saved profile ID from list_profiles. Select per tool call, never bind it to a flow.",
            },
            "condition": {
                "type": "string",
                "enum": ["text", "url", "selector", "network-idle"],
                "description": "Condition for action='wait'. Default: network-idle.",
            },
            "pattern": {
                "type": "string",
                "description": "Text, URL or CSS selector to wait for; required unless network-idle.",
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
            "browser_context": {
                "type": "string",
                "enum": ["isolated", "existing"],
                "description": (
                    "The browser context the user explicitly chose: isolated, or their existing browser/profile."
                ),
            },
            "context_confirmed": {
                "type": "boolean",
                "description": (
                    "True only after the user chose the browser context after being told that existing-browser "
                    "access can expose sessions, cookies, tabs, and extensions."
                ),
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

    def __init__(self, binary_cmd: list[str] | None = None, north_settings: NorthSettings | None = None) -> None:
        self._binary_cmd = binary_cmd
        self._north_settings = north_settings
        self._verified_bindings: dict[tuple[str, str], tuple[str, str]] = {}

    def _profiles(self):
        return self._north_settings.browser_profiles if self._north_settings is not None else []

    def _resolve_profile(self, params: dict) -> tuple[dict, Any]:
        values = dict(params)
        profile_id = values.get("profile_id")
        profiles = self._profiles()
        if not profile_id:
            if profiles:
                raise ValueError("Select profile_id from list_profiles for this browser call; ask_user if unclear.")
            return values, None  # legacy contexts keep their current validation
        profile = next((profile for profile in profiles if profile.id == profile_id and profile.enabled), None)
        if profile is None:
            raise ValueError("Browser profile is missing or disabled. Select an enabled profile from list_profiles.")
        if any(key in values for key in ("connect", "copy_cookies", "browser_context", "context_confirmed", "headed")):
            raise ValueError("A saved profile cannot be overridden by raw connection/context/cookie parameters.")
        values.update(browser_context=profile.context, context_confirmed=True, headed=profile.headed)
        if profile.context == "existing":
            values["connect"] = profile.connect or active_cdp_endpoint(Path(profile.data_directory))
        return values, profile

    @staticmethod
    def _session_args(params: dict, session: str) -> list[str]:
        prefix = ["--json"]
        if session:
            prefix.extend(["--browser", session])
        if params.get("connect"):
            prefix.extend(["--connect", str(params["connect"])])
        if params.get("headed") and not params.get("connect"):
            prefix.append("--headed")
        if params.get("page"):
            prefix.extend(["--page", str(params["page"])])
        return prefix

    def _prefix(self, params: dict, session: str) -> list[str]:
        return [*self._get_cmd(), *self._session_args(params, session)]

    async def _verify_profile(self, profile, params: dict, timeout: int) -> ToolOutput:
        endpoint = str(params["connect"])
        if urlparse(endpoint).scheme in {"ws", "wss"}:
            # Chrome's built-in remote debugging may expose only WebSocket CDP,
            # not /json/version. Let chrome-agent perform its native handshake;
            # only the actual profile-path result below is readiness evidence.
            base = _cdp_http_base(endpoint)
            connection_data = {
                "action": "preflight",
                "endpoint": base,
                "connection_id": hashlib.sha256(endpoint.encode()).hexdigest(),
            }
        else:
            connection = await asyncio.to_thread(_probe_cdp_endpoint, endpoint, timeout)
            if not connection.success:
                return connection
            connection_data = connection.data
        prefix = self._prefix({**params, "page": "north-profile-check"}, profile.browser_name)
        navigation = await _run_chrome_agent([*prefix, "goto", "chrome://version"], timeout)
        if not _tool_output_for("goto", navigation).success:
            return ToolOutput(success=False, error="Could not open the browser profile identity check.")
        result = await _run_chrome_agent(
            [*prefix, "eval", 'document.getElementById("profile_path").textContent'], timeout
        )
        identity = _tool_output_for("eval", result)
        if not identity.success:
            return ToolOutput(success=False, error="Could not verify the connected browser profile.")
        actual = identity.data.get("result", "")
        if not isinstance(actual, str):
            actual = ""
        expected = profile.expected_path if profile.context == "existing" else profile.managed_directory / "Default"
        if not actual or Path(actual).resolve() != expected.resolve():
            return ToolOutput(
                success=False,
                error=(
                    f"Connection reached a different profile than {profile.name}. Select the intended profile in "
                    "Chrome and retry. North will not use another account or copy cookies."
                ),
            )
        key = (str(params.get("task_id") or "default"), profile.id)
        self._verified_bindings[key] = (profile.model_dump_json(), connection_data["connection_id"])
        return ToolOutput(
            success=True,
            data={
                **connection_data,
                "verified": True,
                "profile_id": profile.id,
                "profile_verified": True,
                "login_verified": False,
            },
        )

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        """Classify writes and sensitive existing-browser attachment per call."""
        values = params or {}
        if values.get("action") == "list_profiles":
            return False
        if values.get("profile_id"):
            # Reads can attach to a personal session or launch a managed window.
            # The existing central policy decides, not a separate profile policy.
            return True
        action = str(values.get("action") or "").strip().lower()
        return action in self._MUTATING_ACTIONS or bool(values.get("copy_cookies") or values.get("connect"))

    async def describe(self, input: ToolInput) -> ApprovalRequest:
        """Look at the page first, so the card says what the action lands on (`browser_describe`)."""
        params = input.params or {}
        if params.get("profile_id"):
            from approval.approvals import Request
            from approval.policy import Action, ActionKind

            # No connection, private page read, or profile fallback before the
            # central policy has ruled on this call.
            profile = next((p for p in self._profiles() if p.id == params["profile_id"]), None)
            binding_config = {"browser_profile": profile.model_dump_json() if profile else None}
            config_id = hashlib.sha256(str(binding_config["browser_profile"]).encode()).hexdigest()
            label = profile.name if profile else str(params["profile_id"])
            action = str(params.get("action") or "")
            binding = self._verified_bindings.get((str(params.get("task_id") or "default"), str(params["profile_id"])))
            if (
                profile
                and binding
                and binding[0] == profile.model_dump_json()
                and action not in {"goto", "navigate", "preflight", "status", "close"}
            ):
                try:
                    resolved, _ = self._resolve_profile(params)
                    endpoint = resolved.get("connect") or active_cdp_endpoint(profile.managed_directory)
                    connection = await self._verify_profile(profile, {**resolved, "connect": endpoint}, 5)
                    if connection.success and binding == (profile.model_dump_json(), connection.data["connection_id"]):
                        resolved["page"] = f"task-{str(params.get('task_id') or 'default')}"
                        request = await describe_browser_call(
                            resolved,
                            self._prefix({**resolved, "connect": endpoint}, profile.browser_name),
                            _run_chrome_agent,
                        )
                        return replace(
                            request,
                            title=f"Browser: {label}",
                            message=f"Profile: {label}\n{request.message}",
                            prepared=binding_config,
                            action=replace(
                                request.action,
                                summary=f"{label}: {request.action.summary}",
                                args=f"profile={profile.id}:{config_id}; {request.action.args}",
                            ),
                        )
                except (ValueError, OSError, RuntimeError):
                    pass  # no page peek on an unverified/restarted connection
            return Request(
                action=Action(
                    agent="browser",
                    kind=ActionKind.BROWSER,
                    operation=action,
                    summary=f"{action} using browser profile {label}",
                    args=json.dumps(
                        {**{k: v for k, v in params.items() if k != "task_id"}, "profile_config": config_id},
                        sort_keys=True,
                    ),
                    reaches_third_party=action in {"click", "dblclick", "press", "eval"},
                ),
                title=f"Browser: {label}",
                prepared=binding_config,
                message=f"Use {label} for {action}. Existing profiles expose sessions, cookies, tabs and extensions. "
                "Connection and profile identity will be checked before the requested action.",
            )
        try:
            command = self._get_cmd()
        except RuntimeError:
            command = []  # nothing to look with; the card says north could not read the page
        session = params.get("task_id", "") or params.get("session_id", "default")
        return await describe_browser_call(params, [*command, "--json", "--browser", session], _run_chrome_agent)

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

        args = self._session_args(params, task_id)
        action_params = {key: value for key, value in params.items() if key != "connect"}
        args.extend(build_action_args(action_params))
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
        if action == "list_profiles":
            return ToolOutput(success=True, data={"profiles": [p.catalog_entry() for p in self._profiles()]})
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")
        try:
            params, profile = self._resolve_profile(params)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        if profile is not None and input.approved is not None:
            approved_config = input.approved.prepared
            if (
                not isinstance(approved_config, dict)
                or approved_config.get("browser_profile") != profile.model_dump_json()
            ):
                return ToolOutput(
                    success=False, error="Browser profile changed while awaiting approval. Retry the call."
                )

        browser_context = str(params.get("browser_context") or "").strip().lower()
        if params.get("context_confirmed") is not True or browser_context not in {"isolated", "existing"}:
            return ToolOutput(
                success=False,
                error=(
                    "Browser context is not confirmed. Ask the user which browser profile to use: an isolated browser "
                    "or a specific existing profile through CDP, explaining that existing-browser access can expose "
                    "logged-in sessions, cookies, open tabs, and extensions. Then pass browser_context and "
                    "context_confirmed=true."
                ),
            )
        attaches_to_existing = bool(params.get("connect") or params.get("copy_cookies"))
        if browser_context == "isolated" and attaches_to_existing:
            return ToolOutput(
                success=False,
                error="An isolated browser cannot use connect or copy_cookies from the user's existing profile.",
            )
        if browser_context == "existing" and not attaches_to_existing:
            return ToolOutput(
                success=False,
                error="Existing-browser context requires connect or copy_cookies.",
            )

        # chrome-agent's generic status output describes its own managed
        # browsers; it does not prove that a supplied CDP endpoint is reachable.
        # Perform the DevTools handshake ourselves before claiming attachment.
        if browser_context == "existing" and params.get("connect"):
            if profile is not None:
                try:
                    preflight = await self._verify_profile(profile, params, int(params.get("timeout_seconds", 30)))
                except (RuntimeError, ValueError, OSError) as exc:
                    return ToolOutput(success=False, error=f"Profile verification failed: {exc}")
                if not preflight.success or action in {"preflight", "status"}:
                    return preflight
            else:
                preflight = await asyncio.to_thread(
                    _probe_cdp_endpoint,
                    str(params["connect"]),
                    int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
                )
                if not preflight.success or action in {"preflight", "status"}:
                    return preflight
        elif action == "preflight" and profile is None:
            return ToolOutput(success=False, error="CDP preflight requires browser_context='existing' and connect.")

        unsafe_url = _unsafe_url_reason(action, params)
        if unsafe_url:
            return ToolOutput(success=False, error=unsafe_url)

        session_id = params.get("task_id", "") or params.get("session_id", "default")
        if profile is not None:
            session_id = profile.browser_name
            if action == "close" and profile.context == "existing":
                return ToolOutput(success=False, error="North will not close your existing browser. Close it yourself.")
            params["page"] = f"task-{str(input.params.get('task_id') or 'default')}"
            if action == "preflight":
                params = {**params, "url": "about:blank", "stealth": False}
                action = "goto"
        try:
            command = self._get_cmd() + self._build_args(action, params, session_id)
        except (RuntimeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))

        try:
            navigation = None
            if action == "read" and params.get("url"):
                navigation = _tool_output_for(
                    "goto",
                    await _run_chrome_agent(
                        self._get_cmd() + self._build_args("goto", params, session_id),
                        int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
                    ),
                )
                if not navigation.success:
                    return navigation
            result = await _run_chrome_agent(command, int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)))
        except _ChromeAgentError as exc:
            return ToolOutput(success=False, error=str(exc))

        output = _tool_output_for(action, result)
        if navigation is not None:
            output.data["navigation"] = navigation.data
        if profile is not None:
            output.data.update(profile_id=profile.id, profile_name=profile.name)
            if profile.context == "isolated":
                key = (str(input.params.get("task_id") or "default"), profile.id)
                self._verified_bindings.pop(key, None)
                if output.success and action != "close":
                    with contextlib.suppress(ValueError, OSError):
                        endpoint = active_cdp_endpoint(profile.managed_directory)
                        connection = await asyncio.to_thread(_probe_cdp_endpoint, endpoint, 5)
                        if connection.success:
                            # Store only same-browser evidence for target-aware
                            # descriptions; this is not an approval or a login.
                            self._verified_bindings[key] = (
                                profile.model_dump_json(),
                                hashlib.sha256(endpoint.encode()).hexdigest(),
                            )
            if input.params.get("action") == "preflight" and output.success:
                output.data.update(verified=True, profile_verified=True, login_verified=False)
        return output
