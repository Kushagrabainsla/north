"""BrowserTool — autonomous browser automation and structured web extraction.

Universal tool available to all agents.
Managed sessions use chrome-agent; existing profiles use North's pinned, audited
TaskWindow bridge. Both share this tool and North's approval layer.
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
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import httpx

from config.browser_connection import active_cdp_endpoint
from config.browser_connection import cdp_http_base as _cdp_http_base
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.browser_accounts import BrowserAccounts, Pick, site_of
from tools.universal.browser_describe import describe_and_look, describe_browser_call
from tools.universal.browser_query import query_script
from tools.universal.browser_taskwindow import TaskWindowBrowser, page_chars
from utils.net import HostNotFoundError, UnsafeUrlError, validate_public_url
from utils.processes import stop_process_group
from utils.spans import span

if TYPE_CHECKING:
    from approval.approvals import Request as ApprovalRequest
    from config.browser_profiles import BrowserProfile
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
# Parameters that once attached chrome-agent to the user's own Chrome. Saved
# profiles replaced them; they are refused, never silently ignored.
_RAW_ATTACH_KEYS = ("connect", "copy_cookies")
# The profile check's own fields. Every existing-profile result carried them, a 64-character
# connection_id included, and the model was sent them again on each later turn (issue #83). Only the
# check itself shows them to the model.
_IDENTITY_KEYS = frozenset(
    {"profile_verified", "login_verified", "connection_id", "extension_version", "profile_id", "profile_name"}
)
_IDENTITY_ACTIONS = frozenset({"preflight", "status"})
# Pages one batch read may name (issue #83): read in one call, in parallel tabs where the bridge allows.
_MAX_BATCH_URLS = 10
# A step list (action='steps'): several actions in one call, one card and one model turn. A run made a
# median of 31 browser calls, each after a model turn of about 2.5 s, and each click waited about 4 s for
# its own decision (2026-11-10 to 11-22). A script, an upload or a file is not a step: each is its own call,
# so the card can show it whole.
_STEP_ACTIONS = frozenset(
    {"goto", "navigate", "click", "dblclick", "fill", "type", "press", "select", "check", "uncheck"}
    | {"wait", "query", "assert", "read"}
)
_MAX_STEPS = 10
# A wait in a list waits for its condition, not for a time; a long one stops the list, not the task.
_STEP_WAIT_SECONDS = 15
_STEP_KEYS_REFUSED = ("profile_id", "task_id", "steps", "urls")
_STEP_RESULT_CHARS = 8_000
# What the page shows after an action that changes it, so the model needs no inspect to see the result.
_PAGE_VIEW_LIMIT = 60
_VIEW_AFTER = frozenset({"click", "dblclick", "press", "select", "check", "uncheck"})


def _site(url: str) -> str:
    """The site a URL is on, as a task's approval names it: its host without "www."."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _task_page(task_id: object) -> str:
    """The page a task works in, in a browser that tasks share."""
    return f"task-{str(task_id or 'default')}"


def _profile_ask(label: str, action: str, params: dict[str, Any]) -> str:
    """What the card asks: opening a site covers reading it for the rest of the task, so it says so."""
    if action == "read" and (urls := params.get("urls")):
        sites = sorted({_site(str(url)) for url in urls})
        if len(sites) == 1:
            return (
                f"Open and read {len(urls)} pages on {sites[0]} in {label}. For the rest of this task North can "
                f"read pages on {sites[0]} in this profile without asking again; it asks before other sites and "
                "before clicking, typing, uploading or submitting."
            )
        return f"Open and read {len(urls)} pages in {label}, on {', '.join(sites)}. A yes covers these pages only."
    url = str(params.get("url") or "") if action in _NAVIGATING_ACTIONS else ""
    if site := _site(url):
        return (
            f"Open {site} in {label}. For the rest of this task North can read pages on {site} in this "
            "profile without asking again; it asks before other sites and before clicking, typing, "
            "uploading or submitting."
        )
    return f"Use {label} to {action} on this page."


def _profile_note(profile: BrowserProfile | None) -> str:
    """What the card says about the profile, for its own kind: it described an existing profile for every one."""
    checked = "Connection and profile identity will be checked before the requested action."
    if profile is None or profile.context != "existing":
        return f"A North-managed profile, with its own cookies and logins, apart from your Chrome. {checked}"
    return (
        f"Existing profiles expose sessions, cookies, tabs and extensions. {checked} They work in a background "
        "window of the same profile through North's pinned TaskWindow bridge (Node 20+ and npm required on "
        "first use)."
    )


def _find_chrome_agent_binary() -> list[str] | None:
    """Locate the chrome-agent executable or return an npx fallback command."""
    from config.settings import settings

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
        settings.north_home / "bin" / "chrome-agent",
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
    # chrome-agent drives a Chrome it does not bring: System said "available" on a machine with none, and
    # every call failed "Could not find Chrome or Chromium. Install Chrome and ensure it's on your PATH."
    if why := browser_unavailable():
        return "unavailable", f"chrome-agent is installed but {why}; install Chrome or Chromium"
    if command[0].endswith("npx"):
        return "on demand", "via npx; downloaded on first use"
    return "available", command[0]


# The names chrome-agent finds a browser by on PATH, and the macOS applications.
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
_CHROME_APPS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


def browser_unavailable() -> str:
    """Why no browser can be driven on this machine, or "" when one can: there is no Chrome or Chromium.

    A North-managed profile, a plain session and your existing Chrome all need one; with none, agents
    still offered "I can open it in one of your browser profiles".
    """
    if _find_chrome() is None:
        return f"no Chrome or Chromium was found (looked on PATH for {', '.join(_CHROME_NAMES)})"
    return ""


def _find_chrome() -> str | None:
    """A Chrome or Chromium for chrome-agent to drive, or None."""
    for name in _CHROME_NAMES:
        if found := shutil.which(name):
            return found
    return next((str(app) for app in _CHROME_APPS if app.is_file()), None)


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


def _query_args(params: dict[str, Any]) -> list[str]:
    """`query` is north's own fixed page read (browser_query), run as a script with the call's values as data."""
    return ["eval", query_script(params)]


def _query_output(output: ToolOutput) -> ToolOutput:
    """The query's elements as data: the script hands them back as one JSON string."""
    if not output.success or output.data is None:
        return output
    raw = output.data.pop("result", None)
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return ToolOutput(success=False, data=output.data, error="The page did not return the query's result.")
    if found.get("error"):
        return ToolOutput(success=False, data=output.data, error=str(found["error"]))
    output.data.update(found, action="query")
    return output


def _format_query(data: dict[str, Any]) -> str:
    """One line per element: its text, then the attributes asked for."""
    items = data.get("items") or []
    lines = [f"{data.get('count', len(items))} element(s) match; {len(items)} shown."]
    for number, item in enumerate(items, start=1):
        extra = "; ".join(f"{key}={value}" for key, value in item.items() if key not in {"tag", "text", "visible"})
        hidden = "" if item.get("visible", True) else " (hidden)"
        lines.append(
            f"{number}. <{item.get('tag', '')}>{hidden} {item.get('text', '')}" + (f" [{extra}]" if extra else "")
        )
    return _on_page(data, "\n".join(lines))


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
    "query": _query_args,
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
    """Why this navigation must not happen, or "" when it is safe: every page of a batch read is checked."""
    if action not in _NAVIGATING_ACTIONS:
        return ""
    for url in [params.get("url", ""), *(params.get("urls") or [])]:
        if url and (reason := _unsafe_page(str(url))):
            return reason
    return ""


def _batch_problem(params: dict[str, Any]) -> str:
    """Why a batch read's `urls` cannot be used, or "" when they can (or the call names none)."""
    urls = params.get("urls")
    if urls in (None, []):
        return ""
    if params.get("action") not in (None, "", "read"):
        return "Only action='read' takes 'urls'."
    if not isinstance(urls, list) or not all(isinstance(url, str) and url.strip() for url in urls):
        return "'urls' must be a list of page addresses."
    if len(urls) > _MAX_BATCH_URLS:
        return f"Read at most {_MAX_BATCH_URLS} pages in one call; read the rest in a second call."
    return ""


def _steps_problem(params: dict[str, Any]) -> str:
    """Why a step list cannot run, or "" when it can (or the call is not one)."""
    steps = params.get("steps")
    if params.get("action") != "steps":
        return "Only action='steps' takes 'steps'." if steps not in (None, []) else ""
    if not isinstance(steps, list) or not steps:
        return "action='steps' needs 'steps': a list of actions, e.g. [{'action': 'click', 'uid': 'n12'}]."
    if len(steps) > _MAX_STEPS:
        return f"A list has at most {_MAX_STEPS} steps; send the rest in a second call."
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return f"Step {number} is not an object with an 'action'."
        action = str(step.get("action") or "")
        if action not in _STEP_ACTIONS:
            return (
                f"Step {number}: '{action}' cannot be a step. Steps are {', '.join(sorted(_STEP_ACTIONS))}; "
                "make eval, upload, screenshot and the rest their own calls."
            )
        if refused := [key for key in _STEP_KEYS_REFUSED if key in step]:
            return f"Step {number} names {', '.join(refused)}; give those once, on the call, not on a step."
        if action in _NAVIGATING_ACTIONS and not str(step.get("url") or "").strip():
            return f"Step {number}: '{action}' needs a url."
        if reason := _unsafe_url_reason(action, step):
            return f"Step {number}: {reason}"
    return ""


def _step_target(step: dict[str, Any]) -> str:
    """What a step acts on, in the words of its own params: for a card line and the step's result."""
    action = str(step.get("action") or "")
    if action in _NAVIGATING_ACTIONS:
        return str(step.get("url") or "")
    if action == "wait":
        return f"{step.get('condition') or 'network-idle'} {step.get('pattern') or ''}".strip()
    target = step.get("uid") or step.get("selector") or step.get("xy") or ""
    return str(target)


def _step_line(number: int, step: dict[str, Any], seen: Any = None) -> str:
    """One numbered card line: 'Click "Connect" (button)', 'Fill input#name with "Ada"'."""
    action = str(step.get("action") or "")
    if seen is not None:
        line = seen.title.removeprefix("Browser - ")
    else:
        verb = {"goto": "Open", "navigate": "Open", "wait": "Wait for", "query": "Read", "assert": "Check"}
        line = f"{verb.get(action, action.title())} {_step_target(step)}".strip()
        if action in ("fill", "type", "select") and "value" in step:
            line += f' with "{step["value"]}"'
        if action == "press" and "value" in step:
            line += f" ({step['value']})"
    return f"{number}. {line}"


def _step_result(number: int, step: dict[str, Any], output: ToolOutput) -> dict[str, Any]:
    """A step's outcome as the model reads it: what it was, whether it worked, and what it read."""
    result: dict[str, Any] = {"step": number, "action": step.get("action"), "target": _step_target(step)}
    result["ok"] = output.success
    if not output.success:
        result["error"] = output.error or "the step failed"
    data = output.data or {}
    for key in ("url", "title", "signin_page", "held", "items", "count", "picked_profile"):
        if key in data:
            result[key] = data[key]
    if article := data.get("article"):
        content = str(article.get("content", article.get("text", "")))
        result["text"] = content[:_STEP_RESULT_CHARS]
        result["cut"] = len(content) > _STEP_RESULT_CHARS
    return result


def _format_page_view(page: dict[str, Any]) -> str:
    if page.get("error"):
        return f"### Page now\nNorth could not read the page: {page['error']}"
    head = f"### Page now: {page.get('title') or ''} - {page.get('url') or ''}".rstrip(" -")
    return f"{head}\n```yaml\n{page.get('snapshot') or ''}\n```"


def _format_steps(data: dict[str, Any]) -> str:
    lines = []
    for step in data.get("steps", []):
        mark = "ok" if step.get("ok") else "FAILED"
        line = f"{step['step']}. {step.get('action')} {step.get('target') or ''}".rstrip() + f": {mark}"
        if step.get("error"):
            line += f" - {step['error']}"
        lines.append(line)
        extra = {k: v for k, v in step.items() if k not in ("step", "action", "target", "ok", "error", "text", "cut")}
        if extra:
            lines.append(f"   {json.dumps(extra)}")
        if step.get("text"):
            lines.append(f"   Text{' (cut)' if step.get('cut') else ''}:\n{step['text']}")
    if (skipped := data.get("not_run")) is not None and skipped:
        lines.append(f"Steps {', '.join(str(n) for n in skipped)} did not run.")
    text = "### Steps\n" + "\n".join(lines)
    if page := data.get("page"):
        text += "\n\n" + _format_page_view(page)
    return text


def _unsafe_page(url: str) -> str:
    """Why opening *url* must not happen, or ""."""
    # Loopback stays reachable so agents can drive a locally served app under test.
    if urlparse(url).hostname in _LOOPBACK_HOSTNAMES:
        return ""
    try:
        validate_public_url(url)
    except HostNotFoundError as exc:
        return f"Network failure, not a security refusal: {exc}. This machine cannot look up that address."
    except UnsafeUrlError as exc:
        return f"Security policy blocked navigation: {exc}"
    return ""


async def _run_chrome_agent(command: list[str], timeout: int) -> _CommandResult:
    """One chrome-agent process: started, run and stopped for each browser step."""
    with span("chrome_agent"):
        return await _run_chrome_agent_process(command, timeout)


async def _run_chrome_agent_process(command: list[str], timeout: int) -> _CommandResult:
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
        raise _ChromeAgentError(f"browser timed out after {timeout}s.") from None
    except Exception as exc:
        raise _ChromeAgentError(f"Subprocess execution failed: {exc}") from exc
    finally:
        # The whole group: chrome-agent leaves a browser behind otherwise, on a
        # timeout or when the task is cancelled.
        await stop_process_group(proc, grace_seconds=0)

    return _CommandResult(
        returncode=proc.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
        stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
    )


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


def _format_pages(pages: list[dict[str, Any]]) -> str:
    """A batch read's pages, in the order asked, each under its title and URL."""
    parts = []
    for number, page in enumerate(pages, 1):
        if page.get("error"):
            parts.append(f"## {number}. {page['url']}\nCould not read it: {page['error']}")
            continue
        cut = "\n(Cut to fit beside the other pages: read this URL alone for the rest.)" if page.get("cut") else ""
        title = page.get("title") or page["url"]
        parts.append(f"## {number}. {title}\nURL: {page['url']}\n\n{page.get('content', '')}{cut}")
    return "\n\n".join(parts)


def _on_page(data: dict[str, Any], text: str) -> str:
    """*text* after the URL it was read on, when the result names one."""
    url = data.get("url") or (data.get("navigation") or {}).get("url") or ""
    return f"URL: {url}\n{text}" if url else text


def _format_assertion(data: dict[str, Any]) -> str:
    outcome = "PASS" if data.get("held", True) else "FAIL"
    return f"Assertion [{outcome}]: {json.dumps(data)}"


class BrowserTool(Tool):
    """Automate Chrome and extract structured web data via Chrome DevTools Protocol."""

    name = "browser"
    is_mutating = False
    model_reads_formatted = True
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
            "upload",
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
        "Call list_profiles to see saved names, purposes and the sites each profile is signed in on. Select "
        "profile_id for each tool call from the current task context and configured purposes; profiles belong "
        "to tool calls, not flows. If you leave profile_id out, North picks the profile it saw signed in on the "
        "site (or the one you browse it in, or the one the task's page is open in) and says which in "
        "'picked_profile'; when no profile stands out, the call is refused and you ask. "
        "If the choice is missing or ambiguous, ask which browser profile to use (for example Personal, "
        "University, Work, or an isolated North browser), unless the user already explicitly chose for "
        "this task. Choosing 'existing browser' alone does not identify a profile. Explain that "
        "existing-browser access can expose logged-in sessions, cookies, open tabs, and extensions; "
        "route questions through ask_user and actions through the ordinary approval layer; the mode "
        "decides who answers. Never silently switch profiles or copy cookies as a fallback. "
        "Existing profiles run through North's own bridge in a background window of that same Chrome "
        "profile; nothing is brought to the front, so never ask the user to select or focus a window. "
        "Open the page directly: every call checks the profile's connection and identity first, so do not "
        "call preflight before it - that is one more approval for the user. Use preflight only to diagnose "
        "a failed connection or when the user asks to test one. "
        "If preflight says the profile is not connected, stop and ask_user with requires_user_action=true "
        "to connect it once from the dashboard's Browser profiles; then retry preflight, not the interrupted action. "
        "Reuse it for "
        "the whole task and pauses; never navigate or close the user's unrelated tabs. Windows share "
        "profile logins/cookies and are not separate accounts. When the task ends, North saves a screenshot "
        "of the task's tab in the task folder and closes the tab; a draft left for the user stays open while "
        "its request_approval (wait=false) card waits. close affects only the task's North-owned tab, never "
        "Chrome itself. "
        "If an action's outcome is unknown, inspect and ask for review; never repeat it automatically. "
        "Check the expected account on the target site; if already signed in, "
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
        "  - 'steps': Several actions in ONE call, run in order in the task's tab: 'steps' is a list of up to "
        "10 objects, each with its own 'action' (goto, click, dblclick, fill, type, press, select, check, "
        "uncheck, wait, query, assert, read) and that action's fields. Prefer it whenever you know the next "
        "few actions, e.g. [{'action': 'fill', 'selector': 'input[name=q]', 'value': 'north'}, {'action': "
        "'click', 'selector': 'button[type=submit]'}, {'action': 'wait', 'condition': 'text', 'pattern': "
        "'Results'}]. The whole list is one approval and one model turn. It stops at the first step that "
        "fails, and before any click or key press that would send something to someone or spend money: "
        "make that step its own call. A wait in a list waits for its condition (text, url, selector), at "
        "most 15 s. The result lists each step and shows the page at the end. uids come from the current "
        "page: after a goto in the list, name later targets by selector.\n"
        "  - 'goto' (or 'navigate'): Navigate to URL.\n"
        "  - 'extract': Structured records; existing Chrome supports HTML tables; read/inspect for other listings.\n"
        "  - 'read': Reader-mode text of a page (url), or of up to 10 pages at once (urls), which load in "
        "parallel: when you need several pages, read them all in one call.\n"
        "  - 'inspect': Accessibility Tree (AXTree) with stable numeric UIDs (e.g. n12), or diff=True.\n"
        "  - 'query': Read elements by CSS selector: their text and the attributes you name (e.g. "
        "selector='main button', attributes=['aria-label'], contains='Connect', visible_only=true). It only "
        "reads, so it needs no approval once the site may be read. Use it, not eval, to read a page.\n"
        "  - 'click', 'dblclick': Click element by UID (e.g. uid='n12'), selector, or coordinates. A click, key "
        "press, select or check shows the page after it in 'page', so do not inspect just to see the result.\n"
        "  - 'fill', 'type', 'press': Enter text into input fields or send key presses.\n"
        "  - 'select', 'check', 'uncheck': Manipulate dropdowns and checkboxes.\n"
        "  - 'screenshot', 'pdf': Capture viewport or full page to a file.\n"
        "  - 'eval': Execute JavaScript in the page context, for what no other action does. Each script is "
        "checked before it runs, as it could change the page. In existing profiles use top-level await; "
        "a returned Promise is not awaited.\n"
        "  - 'upload': Existing profiles only. Attach the local file 'filename' to a file input (uid or selector).\n"
        "  - 'assert': Deterministically verify page text, values, element existence, or state.\n"
        "  - 'preflight': Verify the selected managed or existing browser's actual profile identity before use.\n"
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
                    "query",
                    "assert",
                    "wait",
                    "close",
                    "status",
                    "preflight",
                    "upload",
                    "steps",
                ],
                "description": "Action to perform in the browser.",
            },
            "steps": {
                "type": "array",
                "maxItems": _MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": sorted(_STEP_ACTIONS)},
                        "url": {"type": "string"},
                        "uid": {"type": "string"},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                        "condition": {"type": "string", "enum": ["text", "url", "selector", "network-idle"]},
                        "pattern": {"type": "string"},
                        "attributes": {"type": "array", "items": {"type": "string"}},
                        "contains": {"type": "string"},
                        "visible_only": {"type": "boolean"},
                        "assert_type": {"type": "string"},
                        "assert_condition": {"type": "string"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["action"],
                },
                "description": (
                    "For action='steps': the actions to run in order, each an object with its own 'action' and "
                    "fields. One call, one approval, one model turn."
                ),
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for goto/navigate/read).",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _MAX_BATCH_URLS,
                "description": (
                    "For read: several pages in one call, read in parallel. Pass every page you need at once, "
                    "not one read per page."
                ),
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
                "description": "CSS selector: what query reads, or a fallback for click, fill, or assert.",
            },
            "attributes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For query: attributes to return for each element, e.g. ['href', 'aria-label'].",
            },
            "contains": {
                "type": "string",
                "description": "For query: keep only elements whose text or aria-label contains this (any case).",
            },
            "visible_only": {"type": "boolean", "description": "For query: keep only elements shown on the page."},
            "chars": {"type": "integer", "description": "For query: characters of text per element (default 300)."},
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
                "description": "Max items to return for extract (default: 25), query (20) or inspect (100).",
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
                "description": "Output path for screenshot or pdf; the local file to attach for upload.",
            },
            "stealth": {
                "type": "boolean",
                "description": "Enable bot-detection bypass patches on navigation.",
                "default": True,
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

    def __init__(
        self,
        binary_cmd: list[str] | None = None,
        north_settings: NorthSettings | None = None,
        existing_browser: TaskWindowBrowser | None = None,
        accounts: BrowserAccounts | None = None,
    ) -> None:
        self._binary_cmd = binary_cmd
        self._north_settings = north_settings
        self._existing_browser = existing_browser
        # Where each profile is signed in, and where you browse (browser_accounts): what picks a profile.
        self._accounts = accounts
        # The profile north picked for a task's call that named none, and why, said once in the call's result.
        self._picked: dict[str, tuple[str, str]] = {}
        # Browsers chrome-agent started for north (isolated profiles, plain sessions), closed by aclose,
        # and the task pages open in them, each closed when its task ends.
        self._started_browsers: set[str] = set()
        self._opened_pages: set[tuple[str, str]] = set()
        self._verified_bindings: dict[tuple[str, str], tuple[str, str]] = {}
        # The site open in each (task, profile) tab: a fact the approval scope of a read names.
        self._open_sites: dict[tuple[str, str], str] = {}

    @property
    def existing_browser(self) -> TaskWindowBrowser | None:
        """The existing-profile bridge, for one-time setup from the dashboard."""
        return self._existing_browser

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
        if any(key in values for key in (*_RAW_ATTACH_KEYS, "headed")):
            raise ValueError("A saved profile cannot be overridden by raw connection/context/cookie parameters.")
        values.update(browser_context=profile.context, headed=profile.headed)
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
        if profile.context == "existing":
            output = await self._native_call(profile, {**params, "action": "preflight", "timeout_seconds": timeout})
            if output.success and output.data.get("profile_verified") is True:
                key = (str(params.get("task_id") or "default"), profile.id)
                self._verified_bindings[key] = (profile.model_dump_json(), output.data["connection_id"])
            return output
        endpoint = str(params.get("connect") or "")
        connection_data = {}
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
        elif endpoint:
            connection = await asyncio.to_thread(_probe_cdp_endpoint, endpoint, timeout)
            if not connection.success:
                return connection
            connection_data = connection.data
        prefix = self._prefix({**params, "page": "north-profile-check"}, profile.browser_name)
        navigation = await self._chrome_agent([*prefix, "goto", "chrome://version"], timeout)
        if not _tool_output_for("goto", navigation).success:
            return ToolOutput(success=False, error="Could not open the browser profile identity check.")
        result = await self._chrome_agent(
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
        if not endpoint:
            # Managed sessions launch through the same vendor commands. The
            # successful identity read is evidence, not an optimistic goto.
            endpoint = active_cdp_endpoint(profile.managed_directory)
            connection_data = {
                "action": "preflight",
                "endpoint": _cdp_http_base(endpoint),
                "connection_id": hashlib.sha256(endpoint.encode()).hexdigest(),
            }
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

    async def unavailable(self, input: ToolInput) -> str:
        why = browser_unavailable()
        if why:
            return f"The browser cannot work here: {why}."
        if problem := _batch_problem(input.params or {}) or _steps_problem(input.params or {}):
            return problem
        # A call that names no saved profile is refused before the approval layer: the memory decider
        # spent 28 s ruling on "84b241fdce825114]" (a stray bracket) before run() refused it (issue #83).
        if (profile_id := (input.params or {}).get("profile_id")) and not any(
            profile.id == profile_id and profile.enabled for profile in self._profiles()
        ):
            return "Browser profile is missing or disabled. Select an enabled profile from list_profiles."
        return ""

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        """Classify writes and any use of a signed-in profile per call; True means the approval layer decides.

        Every call in a profile goes to the approval layer, which remembers what you
        allowed in the task: the yes to opening a site covers reading it for the rest
        of the task (see `_read_scope`). The browser kept that memory itself and it
        failed, so every page asked again (issue #76, W7). Listing profiles never asks.
        """
        values = params or {}
        action = str(values.get("action") or "").strip().lower()
        if action == "list_profiles":
            return False
        if values.get("profile_id"):
            return True
        if action == "steps":
            return any(isinstance(s, dict) and s.get("action") in self._MUTATING_ACTIONS for s in values["steps"])
        return action in self._MUTATING_ACTIONS

    def _note_page(self, key: tuple[str, str], action: str, params: dict[str, Any], success: bool) -> None:
        """Which site the (task, profile) tab shows now, in either kind of profile; a closed tab shows none.

        Only existing profiles recorded it, so in a North-managed one every page asked again (#76, W7).
        """
        if action == "close":
            self._open_sites.pop(key, None)
        elif success and action in _NAVIGATING_ACTIONS and params.get("url"):
            self._open_sites[key] = _site(str(params["url"]))

    def _read_scope(self, params: dict[str, Any], profile_id: str, config_id: str) -> str:
        """What a yes to this call covers in the task: reading its site in this same, unedited profile.

        A click, typing, an upload or a script covers nothing, nor does a preflight, which
        can open the profile in the background; another site is another scope.
        """
        action = str(params.get("action") or "")
        if action in self._MUTATING_ACTIONS or action in {"preflight", "close"}:
            return ""
        url = params.get("url") if action in _NAVIGATING_ACTIONS else None
        key = (str(params.get("task_id") or "default"), profile_id)
        site = _site(str(url)) if url else self._open_sites.get(key, "")
        if action == "read" and params.get("urls"):
            sites = {_site(str(page)) for page in params["urls"]}
            site = sites.pop() if len(sites) == 1 else ""  # pages on several sites: the yes covers only them
        return f"reading pages on {site} in profile {profile_id} ({config_id[:12]})" if site else ""

    def _rule_scope(self, params: dict[str, Any], profile_id: str, config_id: str) -> str:
        """What a standing rule for this call names: reading its site in this profile, or one kind of change there.

        A change is named with the site of the page it lands on, so "browser eval on linkedin.com" never
        covers a script on another site. The profile's settings are part of it: an edited profile has no rules.
        """
        action = str(params.get("action") or "")
        if action in {"preflight", "close", "status", "list_profiles"}:
            return ""
        if action not in self._MUTATING_ACTIONS:
            return self._read_scope(params, profile_id, config_id)
        site = self._open_sites.get((str(params.get("task_id") or "default"), profile_id), "")
        return f"browser {action} on {site} in profile {profile_id} ({config_id[:12]})" if site else ""

    async def describe(self, input: ToolInput) -> ApprovalRequest:
        """Look at the page first, so the card says what the action lands on (`browser_describe`)."""
        with span("browser.describe"):
            return await self._describe(input)

    async def _peek(
        self, profile: BrowserProfile | None, params: dict[str, Any]
    ) -> tuple[ApprovalRequest, bool] | None:
        """What one action would land on, read from the page now, and whether north could read it.

        The same look a single action's card takes (`browser_describe`). None when there is nothing to look with.
        """
        try:
            resolved, _ = self._resolve_profile(params)
            if profile is not None and profile.context == "existing":
                if self._existing_browser is None:
                    return None
                if resolved.get("uid") and not resolved.get("selector"):
                    # The page script then reads the element too: whether it is in a search form.
                    resolved["selector"] = f'[data-north-uid="{resolved["uid"]}"]'
                runner = self._existing_browser.description_runner(profile, resolved)
                return await describe_and_look(resolved, [], runner)
            if profile is not None:
                endpoint = active_cdp_endpoint(profile.managed_directory)
                resolved["page"] = _task_page(params.get("task_id"))
                prefix = self._prefix({**resolved, "connect": endpoint}, profile.browser_name)
                return await describe_and_look(resolved, prefix, _run_chrome_agent)
            session = str(params.get("task_id") or "default")
            return await describe_and_look(
                resolved, [*self._get_cmd(), "--json", "--browser", session], _run_chrome_agent
            )
        except (ValueError, OSError, RuntimeError, httpx.HTTPError):
            return None

    async def _describe_steps(self, params: dict[str, Any]) -> ApprovalRequest:
        """One card for a whole step list: each step on its own line, the sites it opens, what a yes covers.

        Steps before the list's first page change are read from the page now, so a click shows its button's
        words, as a single click's card does; later pages cannot be read yet, so those steps show their own
        params. A yes covers exactly this list. The list never sends or spends: it stops before such a step.
        """
        from approval.approvals import Request
        from approval.policy import Action, ActionKind

        task = str(params.get("task_id") or "default")
        profile_id = str(params.get("profile_id") or "")
        profile = next((p for p in self._profiles() if p.id == profile_id), None)
        binding_config = {"browser_profile": profile.model_dump_json() if profile else None}
        config_id = hashlib.sha256(str(binding_config["browser_profile"]).encode()).hexdigest()
        label = profile.name if profile else (profile_id or "North's browser")
        site_now = self._open_sites.get((task, profile_id), "")
        binding = self._verified_bindings.get((task, profile_id))
        may_look = profile is None or bool(binding and binding[0] == profile.model_dump_json())

        steps: list[dict[str, Any]] = params["steps"]
        lines, sites, changes, page_changed = [], set(), False, False
        for number, step in enumerate(steps, 1):
            action = str(step["action"])
            seen = None
            if action in _NAVIGATING_ACTIONS:
                sites.add(_site(str(step["url"])))
                page_changed = True
            elif action in self._MUTATING_ACTIONS:
                changes = True
                if may_look and not page_changed:
                    named = {"profile_id": profile_id} if profile_id else {}
                    looked = await self._peek(profile, {**step, **named, "task_id": task})
                    seen = looked[0] if looked and looked[1] else None
                page_changed = page_changed or action in ("click", "dblclick", "press")
            lines.append(_step_line(number, step, seen))
        if site_now and not any(str(s["action"]) in _NAVIGATING_ACTIONS for s in steps[:1]):
            sites.add(site_now)
        where = ", ".join(sorted(s for s in sites if s)) or "the current page"
        message = (
            f"Run {len(steps)} steps in {label} on {where}:\n"
            + "\n".join(lines)
            + "\nNorth runs them in order and stops at the first step that fails. It stops before any click "
            "or key press that would send something to someone or spend money: that step needs its own card."
        )
        if profile is not None:
            message += f"\n{_profile_note(profile)}"
        scope = ""
        if profile is not None and not changes and len(sites) == 1:
            scope = f"reading pages on {next(iter(sites))} in profile {profile_id} ({config_id[:12]})"
        return Request(
            action=Action(
                agent="browser",
                kind=ActionKind.BROWSER,
                operation="steps",
                summary=f"{label}: {len(steps)} browser steps on {where}",
                command=where,
                args=json.dumps({"steps": steps, "profile_config": config_id}, sort_keys=True),
                scope=scope,
                details=message,
            ),
            title=f"Browser: {label} - {len(steps)} steps",
            prepared=binding_config if profile is not None else None,
            message=message,
        )

    async def _describe(self, input: ToolInput) -> ApprovalRequest:
        params = input.params or {}
        if params.get("action") == "steps":
            return await self._describe_steps(params)
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
            scope = self._read_scope(params, str(params["profile_id"]), config_id)
            rule_scope = self._rule_scope(params, str(params["profile_id"]), config_id)
            binding = self._verified_bindings.get((str(params.get("task_id") or "default"), str(params["profile_id"])))
            if (
                profile
                and binding
                and binding[0] == profile.model_dump_json()
                and action not in {"goto", "navigate", "preflight", "status", "close"}
                and not params.get("urls")
            ):
                try:
                    resolved, _ = self._resolve_profile(params)
                    if profile.context == "existing" and self._existing_browser is not None:
                        identity = await self._existing_browser.verify(profile, wait=False)
                        connection_id = identity["connection_id"]
                    else:
                        endpoint = active_cdp_endpoint(profile.managed_directory)
                        connection = await self._verify_profile(profile, {**resolved, "connect": endpoint}, 5)
                        connection_id = connection.data.get("connection_id") if connection.success else None
                    if connection_id and binding == (profile.model_dump_json(), connection_id):
                        if profile.context == "existing" and self._existing_browser is not None:
                            request = await describe_browser_call(
                                resolved, [], self._existing_browser.description_runner(profile, resolved)
                            )
                        else:
                            resolved["page"] = _task_page(params.get("task_id"))
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
                                scope=scope,
                                rule_scope=rule_scope,
                            ),
                        )
                except (ValueError, OSError, RuntimeError, httpx.HTTPError):
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
                    scope=scope,
                    rule_scope=rule_scope,
                ),
                title=f"Browser: {label}",
                prepared=binding_config,
                message=f"{_profile_ask(label, action, params)} {_profile_note(profile)}",
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
        """The one copy of a result the model reads (`model_reads_formatted`)."""
        action = data.get("action", "")
        shown = data if action in _IDENTITY_ACTIONS else {k: v for k, v in data.items() if k not in _IDENTITY_KEYS}
        if action == "steps":
            return _format_steps(data)
        if page := shown.pop("page", None):
            return json.dumps(shown) + "\n\n" + _format_page_view(page)
        if action == "extract" and "items" in data:
            return _format_records(data)
        if action == "read" and "pages" in data:
            return _format_pages(data["pages"])
        if action == "read" and "article" in data:
            return _on_page(data, _format_article(data["article"]))
        tree = data.get("tree") or data.get("snapshot")
        if action == "inspect" and tree:
            return _on_page(data, f"### Accessibility Tree:\n```yaml\n{tree}\n```")
        if action == "assert":
            return _format_assertion(shown)
        if action == "query" and "items" in data:
            return _format_query(data)
        return json.dumps(shown)

    async def _read_each(self, input: ToolInput, urls: list[str]) -> ToolOutput:
        """A batch read in North's own browser: each page in turn, still one call and one model turn.

        chrome-agent drives one page of a task at a time; your existing Chrome reads them in parallel.
        """
        chars = page_chars(len(urls))
        pages: list[dict[str, Any]] = []
        for url in urls:
            single = {key: value for key, value in input.params.items() if key != "urls"} | {"url": url}
            output = await self.run(input.model_copy(update={"params": single}))
            if not output.success:
                pages.append({"url": url, "error": output.error or "the page could not be read"})
                continue
            article = output.data.get("article") or {}
            content = str(article.get("content", article.get("text", "")))
            pages.append(
                {"url": url, "title": article.get("title", ""), "content": content[:chars], "cut": len(content) > chars}
            )
        return ToolOutput(success=True, data={"action": "read", "pages": pages})

    async def _native_call(self, profile, params: dict) -> ToolOutput:
        if self._existing_browser is None:
            return ToolOutput(
                success=False, error="The existing-Chrome adapter is not configured. No fallback is allowed."
            )
        return await self._existing_browser.call(profile, params)

    def _catalog_entry(self, profile: BrowserProfile) -> dict[str, Any]:
        """A profile as list_profiles shows it, with the sites north saw it signed in on."""
        entry = profile.catalog_entry()
        if self._accounts is not None and (sites := self._accounts.signed_in_sites(profile.id)):
            entry["signed_in_on"] = sites
        return entry

    async def prepare(self, input: ToolInput) -> tuple[ToolInput, str, ApprovalRequest | None]:
        """The call as it runs, with the profile north picks when it names none, decided before any card."""
        return await super().prepare(await self._with_picked_profile(input))

    async def _with_picked_profile(self, input: ToolInput) -> ToolInput:
        """*input* with a profile_id when the call names none and one profile stands out (browser_accounts).

        A page opened on a site: the one profile seen signed in there, else the one you browse it in.
        Any other call: the profile the task's page is open in. With no answer, the call goes on
        without one and is refused with "Select profile_id", so the agent asks you.
        """
        params = input.params or {}
        profiles = self._profiles()
        action = str(params.get("action") or ("goto" if params.get("url") else ""))
        if params.get("profile_id") or not profiles or action in {"", "list_profiles"}:
            return input
        task = str(params.get("task_id") or "default")
        urls = [str(url) for url in params.get("urls") or []] or ([str(params["url"])] if params.get("url") else [])
        pick: Pick | None = None
        if action in _NAVIGATING_ACTIONS and urls and self._accounts is not None:
            if len({site_of(url) for url in urls}) == 1:
                await asyncio.to_thread(self._accounts.refresh_history, [p for p in profiles if p.enabled])
                pick = await asyncio.to_thread(self._accounts.pick, urls[0], profiles)
        elif not urls:
            open_in = {profile_id for task_id, profile_id in self._open_sites if task_id == task}
            if len(open_in) == 1:
                pick = Pick(open_in.pop(), "the profile this task's page is open in")
        if pick is None:
            return input
        self._picked[task] = (pick.profile_id, pick.why)
        return input.model_copy(update={"params": {**params, "profile_id": pick.profile_id}})

    def _say_picked(self, task: str, profile: BrowserProfile, output: ToolOutput) -> None:
        """Tell the agent, once, which profile north picked for it and why."""
        picked = self._picked.get(task)
        if picked and picked[0] == profile.id:
            del self._picked[task]
            output.data["picked_profile"] = f"{profile.name} ({profile.id}): {picked[1]}"

    def _learn_page(self, profile: BrowserProfile, data: dict[str, Any]) -> None:
        """Keep what the pages showed about signing in, for picking a profile next time."""
        if self._accounts is None:
            return
        listed = data.get("pages")
        pages: list[Any] = listed if isinstance(listed, list) else [data]
        for page in pages:
            if isinstance(page, dict) and "signin_page" in page and page.get("url"):
                self._accounts.record(
                    str(page["url"]),
                    profile.id,
                    signed_in=not page["signin_page"],
                    account=str(page.get("account") or ""),
                )

    async def _native_action(self, profile, params: dict, input: ToolInput) -> ToolOutput:
        if params.get("action") == "upload":
            from tools._path import resolve_path

            source = resolve_path(str(params.get("filename") or ""), input.granted_workspace, contained=False)
            if source is None or not source.is_file():
                return ToolOutput(success=False, error="Upload needs 'filename': an existing local file.")
            params = {**params, "filename": str(source)}
        if params.get("action") in {"screenshot", "pdf"}:
            from tools._path import resolve_path, scope_refusal

            path = resolve_path(str(params.get("filename") or ""), input.granted_workspace)
            refusal = scope_refusal(input.edit_scope, path) if path is not None else "Output path is not allowed."
            if not params.get("filename") or refusal:
                return ToolOutput(success=False, error=refusal or "A workspace filename is required.")
            params = {**params, "filename": str(path)}
        output = await self._native_call(profile, params)
        output.data.update(profile_id=profile.id, profile_name=profile.name)
        if output.success:
            self._learn_page(profile, output.data)
            self._say_picked(str(params.get("task_id") or "default"), profile, output)
        key = (str(params.get("task_id") or "default"), profile.id)
        if output.success and output.data.get("profile_verified") is True:
            self._verified_bindings[key] = (profile.model_dump_json(), output.data["connection_id"])
        self._note_page(key, str(params.get("action") or ""), params, output.success)
        if params.get("action") == "close":
            self._verified_bindings.pop(key, None)
        return output

    async def _chrome_agent(self, command: list[str], timeout: int) -> _CommandResult:
        """Run chrome-agent, remembering the browser and page the command opens so north can close them."""
        if "--browser" in command:
            name = command[command.index("--browser") + 1]
            page = command[command.index("--page") + 1] if "--page" in command else ""
            closing = command[-1] == "close"
            if closing and not page:
                self._started_browsers.discard(name)
                self._opened_pages = {opened for opened in self._opened_pages if opened[0] != name}
            else:
                self._started_browsers.add(name)
            if closing and page:
                self._opened_pages.discard((name, page))
            elif page:
                self._opened_pages.add((name, page))
        return await _run_chrome_agent(command, timeout)

    async def _close(self, *target: str) -> None:
        """Close a browser or page north opened; one that will not close is logged, not raised."""
        try:
            await _run_chrome_agent([*self._get_cmd(), "--json", *target, "close"], 15)
        except (_ChromeAgentError, RuntimeError):
            logger.warning("could not close %s, which north opened", " ".join(target), exc_info=True)

    async def end_task(self, task_id: str) -> None:
        """Close what *task_id* opened: its page in each North-managed profile, its own browser, its tabs in yours.

        They stayed open until north stopped, a tab for every task, one holding a URL
        with a secret in it (issue #76, W1). Tabs in your existing Chrome stayed for
        good and piled up in North's tab group (issue #83).
        """
        await self._finish_existing_tabs(task_id)
        page = _task_page(task_id)
        pages = sorted(opened for opened in self._opened_pages if opened[1] == page)
        self._opened_pages.difference_update(pages)
        for browser, _ in pages:
            await self._close("--browser", browser, "--page", page)
        if task_id in self._started_browsers:  # a plain session's browser is named after its task
            self._started_browsers.discard(task_id)
            await self._close("--browser", task_id)

    async def _finish_existing_tabs(self, task_id: str) -> None:
        """Save each tab the task left open in your Chrome as a screenshot in the task's folder, then close it.

        The screenshot is the record of what north saw last; it is never sent to a model. A tab stays open
        while a card of the task still waits for you, such as a draft handed over to send yourself.
        """
        if self._existing_browser is None:
            return
        interaction = self._approvals().interaction
        if interaction is not None and interaction.waiting_on_you(task_id):
            return
        from utils.handoff import handoff_dir_for

        for key in [key for key in self._open_sites if key[0] == task_id]:
            profile = next((p for p in self._profiles() if p.id == key[1] and p.context == "existing"), None)
            if profile is None:
                continue
            self._open_sites.pop(key, None)
            self._verified_bindings.pop(key, None)
            screenshot = Path(handoff_dir_for(task_id)) / f"browser-{profile.id}.png"
            if await self._existing_browser.finish(profile, task_id, screenshot):
                logger.info(
                    "Closed task %s's tab in %s; its last page is saved in %s", task_id, profile.name, screenshot
                )

    async def aclose(self) -> None:
        """Close every browser north started; disconnect existing Chrome without closing the user's browser.

        chrome-agent starts Chrome as a daemon outside its own process group, so
        stopping chrome-agent left it running. In the issue #76 test one headless
        Chromium outlived three `north stop`s, with an unauthenticated DevTools
        port and a tab for every task, one holding a URL with a secret in it.
        """
        for name in sorted(self._started_browsers):
            await self._close("--browser", name)
        self._started_browsers.clear()
        self._opened_pages.clear()
        if self._existing_browser is not None:
            await self._existing_browser.aclose()

    async def _page_view(self, input: ToolInput, base: dict[str, Any]) -> dict[str, Any]:
        """The page as it is now, short: its url, title and the elements one can act on, with their uids."""
        view = await self.run(
            input.model_copy(update={"params": {**base, "action": "inspect", "limit": _PAGE_VIEW_LIMIT, "_step": True}})
        )
        if not view.success:
            return {"error": view.error or "the page could not be read"}
        data = view.data or {}
        snapshot = data.get("snapshot") or data.get("tree") or ""  # chrome-agent names it tree
        return {"url": data.get("url", ""), "title": data.get("title", ""), "snapshot": snapshot}

    async def _step_stop(self, profile: BrowserProfile | None, params: dict[str, Any]) -> str:
        """Why a list must stop before this step, or "": it would send or spend, or north cannot see it.

        A list's card cannot show the pages it has not reached, so a send or a spend is never part of one.
        The step is read from the page as it is now, as a single action's card reads it.
        """
        if params["action"] not in ("click", "dblclick", "press"):
            return ""
        looked = await self._peek(profile, params)
        if looked is None or not looked[1]:
            return "North could not see what this step lands on, so it did not run it. Make it its own call."
        request = looked[0]
        if request.action.reaches_third_party or request.action.spends_money:
            what = "spend money" if request.action.spends_money else "send something to someone"
            return (
                f"This step would {what} ({request.title.removeprefix('Browser - ')}), so the list stopped before "
                "it. Make it its own call, so it gets its own card."
            )
        return ""

    async def _run_steps(self, input: ToolInput, params: dict[str, Any], profile: BrowserProfile | None) -> ToolOutput:
        """Run an approved step list in order, in the task's tab; stop at the first step that fails or must stop."""
        base = {key: params[key] for key in ("profile_id", "task_id") if params.get(key)}
        steps: list[dict[str, Any]] = params["steps"]
        results: list[dict[str, Any]] = []
        failed = ""
        for number, step in enumerate(steps, 1):
            step_params = {**base, **step, "_step": True}
            if step["action"] == "wait":
                asked = int(step.get("timeout_seconds") or _STEP_WAIT_SECONDS)
                step_params["timeout_seconds"] = min(max(asked, 1), _STEP_WAIT_SECONDS)
            with span("steps.check"):
                stop = await self._step_stop(profile, step_params)
            if stop:
                results.append({"step": number, "action": step["action"], "target": _step_target(step), "ok": False})
                results[-1]["error"] = stop
                failed = f"Step {number} did not run: {stop}"
                break
            output = await self.run(input.model_copy(update={"params": step_params}))
            results.append(_step_result(number, step, output))
            if not output.success:
                failed = f"Step {number} ({step['action']}) failed: {output.error or 'the step failed'}"
                break
        data: dict[str, Any] = {
            "action": "steps",
            "steps": results,
            "not_run": list(range(len(results) + 1, len(steps) + 1)),
        }
        if profile is not None:
            data.update(profile_id=profile.id, profile_name=profile.name)
        data["page"] = await self._page_view(input, base)
        return ToolOutput(success=not failed, data=data, error=failed or None)

    async def run(self, input: ToolInput) -> ToolOutput:
        params = dict(input.params or {})
        in_list = bool(params.pop("_step", False))
        input = input.model_copy(update={"params": params})
        output = await self._run_one(input)
        action = params.get("action", "") or ("goto" if params.get("url") else "")
        if action in _VIEW_AFTER and output.success and not in_list:
            base = {key: params[key] for key in ("profile_id", "task_id") if params.get(key)}
            output.data["page"] = await self._page_view(input, base)
        return output

    async def _run_one(self, input: ToolInput) -> ToolOutput:
        params = input.params or {}
        action = params.get("action", "") or ("goto" if params.get("url") else "")
        if action == "list_profiles":
            return ToolOutput(success=True, data={"profiles": [self._catalog_entry(p) for p in self._profiles()]})
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")
        if any(key in params for key in _RAW_ATTACH_KEYS):
            return ToolOutput(
                success=False,
                error="North reaches your own browser only through a saved profile: pass a profile_id.",
            )
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

        if action == "steps":
            if problem := _steps_problem(params):
                return ToolOutput(success=False, error=problem)
            return await self._run_steps(input, params, profile)

        if profile is not None and profile.context == "existing":
            unsafe_url = _unsafe_url_reason(action, params)
            if unsafe_url:
                return ToolOutput(success=False, error=unsafe_url)
            return await self._native_action(profile, {**params, "action": action}, input)
        if action == "upload":
            return ToolOutput(success=False, error="Upload is available only in existing browser profiles.")

        unsafe_url = _unsafe_url_reason(action, params)
        if unsafe_url:
            return ToolOutput(success=False, error=unsafe_url)

        if action == "read" and params.get("urls"):
            return await self._read_each(input, list(params["urls"]))

        if action == "preflight" and profile is None:
            return ToolOutput(success=False, error="Preflight checks a saved profile: pass profile_id.")
        if action == "preflight":
            try:
                return await self._verify_profile(profile, params, int(params.get("timeout_seconds", 30)))
            except (RuntimeError, ValueError, OSError) as exc:
                return ToolOutput(success=False, error=f"Profile verification failed: {exc}")

        session_id = params.get("task_id", "") or params.get("session_id", "default")
        if profile is not None:
            session_id = profile.browser_name
            params["page"] = _task_page(input.params.get("task_id"))
        try:
            command = self._get_cmd() + self._build_args(action, params, session_id)
        except (RuntimeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))

        try:
            navigation = None
            if action == "read" and params.get("url"):
                navigation = _tool_output_for(
                    "goto",
                    await self._chrome_agent(
                        self._get_cmd() + self._build_args("goto", params, session_id),
                        int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
                    ),
                )
                if not navigation.success:
                    return navigation
            result = await self._chrome_agent(command, int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)))
        except _ChromeAgentError as exc:
            return ToolOutput(success=False, error=str(exc))

        output = _tool_output_for(action, result)
        if action == "query":
            output = _query_output(output)
        if navigation is not None:
            output.data["navigation"] = navigation.data
        if profile is not None:
            output.data.update(profile_id=profile.id, profile_name=profile.name)
            key = (str(input.params.get("task_id") or "default"), profile.id)
            self._note_page(key, action, params, output.success)
            if profile.context == "isolated":
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
        return output
