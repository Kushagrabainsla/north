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


class BrowserTool(Tool):
    """Automate Chrome and extract structured web data via Chrome DevTools Protocol."""

    name = "browser"
    is_mutating = False
    description = (
        "Autonomous browser automation and structured web extraction via Chrome CDP. "
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
        args: list[str] = ["--json"]
        if task_id:
            args.extend(["--browser", task_id])

        if action in ("goto", "navigate"):
            url = params.get("url", "").strip()
            if not url:
                raise ValueError("Parameter 'url' is required for action='goto'.")
            args.extend(["goto", url])
            if params.get("stealth", True):
                args.append("--stealth")
            if params.get("copy_cookies"):
                args.append("--copy-cookies")
            if params.get("connect"):
                args.extend(["--connect", str(params["connect"])])
            if params.get("inspect", True):
                args.append("--inspect")

        elif action == "inspect":
            if params.get("diff"):
                args.append("diff")
            else:
                args.append("inspect")
                limit = params.get("limit", _DEFAULT_INSPECT_LIMIT)
                args.extend(["--limit", str(limit)])
                if params.get("uid"):
                    args.extend(["--uid", str(params["uid"])])

        elif action == "extract":
            args.append("extract")
            limit = params.get("limit", _DEFAULT_EXTRACT_LIMIT)
            args.extend(["--limit", str(limit)])
            if params.get("query"):
                args.extend(["--query", str(params["query"])])

        elif action == "read":
            url = params.get("url", "").strip()
            if url:
                args.extend(["goto", url, "--inspect"])
            args.append("read")

        elif action in ("click", "dblclick"):
            args.append(action)
            if params.get("uid"):
                args.append(str(params["uid"]))
            elif params.get("selector"):
                args.extend(["--selector", str(params["selector"])])
            elif params.get("xy"):
                args.extend(["--xy", str(params["xy"])])
            else:
                raise ValueError(f"Action '{action}' requires 'uid', 'selector', or 'xy'.")
            args.append("--inspect")

        elif action == "fill":
            val = str(params.get("value", ""))
            args.append("fill")
            if params.get("uid"):
                args.extend(["--uid", str(params["uid"]), val])
            elif params.get("selector"):
                args.extend(["--selector", str(params["selector"]), val])
            else:
                raise ValueError("Action 'fill' requires 'uid' or 'selector'.")
            args.append("--inspect")

        elif action == "type":
            val = str(params.get("value", ""))
            args.extend(["type", val])
            if params.get("selector"):
                args.extend(["--selector", str(params["selector"])])

        elif action == "press":
            key = str(params.get("value", "Enter"))
            args.extend(["press", key])

        elif action in ("select", "check", "uncheck"):
            args.append(action)
            if params.get("uid"):
                args.extend(["--uid", str(params["uid"])])
            elif params.get("selector"):
                args.extend(["--selector", str(params["selector"])])
            if action == "select" and "value" in params:
                args.append(str(params["value"]))

        elif action == "screenshot":
            args.append("screenshot")
            if params.get("filename"):
                args.extend(["--filename", str(params["filename"])])
            if params.get("uid"):
                args.extend(["--uid", str(params["uid"])])
            elif params.get("selector"):
                args.extend(["--selector", str(params["selector"])])

        elif action == "pdf":
            args.append("pdf")
            if params.get("filename"):
                args.extend(["--filename", str(params["filename"])])

        elif action == "eval":
            js = params.get("js", "").strip()
            if not js:
                raise ValueError("Parameter 'js' is required for action='eval'.")
            args.extend(["eval", js])

        elif action == "assert":
            assert_type = params.get("assert_type", "text")
            condition = params.get("assert_condition", "contains")
            val = params.get("value", "")
            args.extend(["assert", assert_type])
            if params.get("uid"):
                args.extend(["--uid", str(params["uid"])])
            elif params.get("selector"):
                args.extend(["--selector", str(params["selector"])])

            if condition:
                args.append(f"--{condition}")
            if val:
                args.append(str(val))

        elif action == "wait":
            cond = params.get("condition", "network-idle")
            args.extend(["wait", cond])

        elif action == "close":
            args.extend(["close", "--purge"])

        elif action == "status":
            args.append("status")

        else:
            raise ValueError(f"Unknown action '{action}'.")

        return args

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action", "")

        # Format extract structured records as a Markdown table
        if action == "extract" and "items" in data:
            items = data["items"]
            count = data.get("count", len(items))
            if not items:
                return "No structured records found on page."

            # Find column keys from the first few items
            keys = []
            for item in items[:5]:
                for k in item:
                    if k not in keys:
                        keys.append(k)

            sep = "| " + " | ".join(["---"] * len(keys)) + " |"
            header = "| " + " | ".join(keys) + " |"
            lines = [f"### Extracted {count} Records:", "", header, sep]
            for item in items:
                row = []
                for k in keys:
                    val = str(item.get(k, "")).replace("|", "\\|").replace("\n", " ")
                    if len(val) > 80:
                        val = val[:77] + "…"
                    row.append(val)
                lines.append("| " + " | ".join(row) + " |")
            return "\n".join(lines)

        # Format reader mode output
        if action == "read" and "article" in data:
            art = data["article"]
            title = art.get("title", "Article")
            byline = f" by {art['byline']}" if art.get("byline") else ""
            content = art.get("content", art.get("text", ""))
            return f"## {title}{byline}\n\n{content}"

        # Format inspect tree
        if action == "inspect" and "tree" in data:
            return f"### Accessibility Tree:\n```yaml\n{data['tree']}\n```"

        # Format assertion outcome
        if action == "assert":
            held = data.get("held", True)
            status_text = "PASS" if held else "FAIL"
            return f"Assertion [{status_text}]: {json.dumps(data)}"

        # Default pretty JSON
        return json.dumps(data, indent=2)

    async def run(self, input: ToolInput) -> ToolOutput:
        params = input.params or {}
        action = params.get("action", "")
        if not action:
            if params.get("url"):
                action = "goto"
            else:
                return ToolOutput(success=False, error="Parameter 'action' is required.")

        # SSRF safety validation for external URLs
        url = params.get("url", "")
        if url and action in ("goto", "navigate", "read"):
            parsed = urlparse(url)
            # Allow localhost / 127.0.0.1 for local web app testing in dev
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                try:
                    validate_public_url(url)
                except UnsafeUrlError as exc:
                    return ToolOutput(success=False, error=f"Security policy blocked navigation: {exc}")

        try:
            base_cmd = self._get_cmd()
        except RuntimeError as exc:
            return ToolOutput(success=False, error=str(exc))

        task_id = params.get("task_id", "") or params.get("session_id", "default")
        try:
            cmd_args = self._build_args(action, params, task_id)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        full_cmd = base_cmd + cmd_args
        timeout = int(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

        try:
            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                with contextlib.suppress(Exception):
                    await proc.communicate()
                return ToolOutput(success=False, error=f"browser timed out after {timeout}s.")
        except Exception as exc:
            return ToolOutput(success=False, error=f"Subprocess execution failed: {exc}")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        # Handle deterministic assertion exit code 2 (condition unmet)
        if action == "assert" and proc.returncode == 2:
            return ToolOutput(
                success=False,
                data={"action": "assert", "held": False, "stdout": stdout, "stderr": stderr},
                error=f"Assertion unmet: {stdout or stderr}",
            )

        # Parse JSON output if present
        data: dict[str, Any] = {"action": action}
        hint = ""
        json_error = ""
        if stdout.startswith("{") and stdout.endswith("}"):
            try:
                parsed_json = json.loads(stdout)
                if isinstance(parsed_json, dict):
                    data.update(parsed_json)
                    hint = parsed_json.get("hint", "")
                    json_error = parsed_json.get("error", "")
                    if not parsed_json.get("ok", True) and proc.returncode == 0:
                        err = json_error or "Unknown browser failure."
                        if hint:
                            err += f" (Hint: {hint})"
                        return ToolOutput(success=False, data=data, error=err)
            except Exception:
                data["raw_output"] = stdout
        else:
            data["raw_output"] = stdout

        if proc.returncode != 0:
            error_msg = json_error or stderr or stdout or f"browser exited with status {proc.returncode}."
            if hint:
                error_msg += f" (Hint: {hint})"
            return ToolOutput(
                success=False,
                data=data,
                error=error_msg,
            )

        # Cap output size to protect context window
        if len(stdout) > _MAX_OUTPUT_CHARS:
            data["truncated"] = True

        return ToolOutput(success=True, data=data)
