"""Existing Chrome profiles through the audited, pinned TaskWindow bridge.

One TaskWindow daemon per profile, each on its own port with its own key; the
extension is loaded unpacked from a per-profile folder, so it never auto-updates
and its ID names that folder. Policy stays in BrowserTool and the approval layer:
this adapter only maps North's browser actions onto TaskWindow tools, proves the
profile, and refuses to replay a page action whose outcome is unknown.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx

from tools.models import ToolOutput
from utils.desktop import activate, frontmost_app
from utils.runtime_resources import resource_path

if TYPE_CHECKING:
    from config.browser_profiles import BrowserProfile

logger = logging.getLogger(__name__)

PINNED_VERSION = "0.2.13"
_INSTALL_TIMEOUT_SECONDS = 180
_CONNECT_GRACE_SECONDS = 35  # the extension's reconnect backoff tops out at 30s
_LAUNCH_WAIT_SECONDS = 30
_TASK_GROUP = "North"  # TaskWindow allows one or two words
_READ_ATTEMPTS = 3
# A page that is loading or switching sites detaches Chrome's debugger for a
# moment; TaskWindow reattaches on the next call.
_NAVIGATION_RACES = (
    "Debugger is not attached",
    "Detached while handling command",
    "Inspected target navigated or closed",
    "Execution context was destroyed",
    "Cannot find context with specified id",
)
# Upstream hands the daemon key to any request claiming the Web Store origin; a
# local program can forge that header. North pairs only with one-time codes.
_STORE_PAIRING = (
    "      if (req.headers.origin === STORE_ORIGIN) sendJson(res, 200, { token: config.token });\n"
    '      else sendJson(res, 403, { error: "a pairing code is required" });\n'
)
_CODE_ONLY_PAIRING = '      sendJson(res, 403, { error: "a pairing code is required" });\n'
# Public half of a key added to every profile's copy of the manifest, so all of
# North's copies share one extension ID. Chrome lets only one extension ID debug
# at a time; copies with different IDs would lock each other out. The private
# half was discarded: unpacked extensions are never signed.
_EXTENSION_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwDir6+3ZJNjM0wqJzix8F4zgllW0Kt44VnbQ+95QdiMBuMQdpGpl"
    "niRH1QZBW79Cyp8RzgjilKYcKUr8Dn+zzLVo1pSBikaptyyBg0Nyw3cZjgoZ5lDLl3OkvC6K76yK2go3i3TUoST0d/kW1lGZ"
    "0PWPHOPg0+HnfZekT3m6f3KpcZcEyl66qlhWByWS9ELRZPxYzQQGhsmShTOYxDDDTGcAiCoVRUos8fze2qYkRRBvDysX5nr3"
    "ErYNyoUbVYh71A7MWDPM6v9z7Y3Rs6kpAfC4o8c9SCwDKaVDQTc71z1ca4fnr1cASj3x+XgAMwzYSr3ewdK2mBg6n60kzBEM"
    "pwIDAQAB"
)
# Data directory suffix -> (app name, bundle ID) on macOS.
_BROWSER_APPS = {
    "Google/Chrome": ("Google Chrome", "com.google.Chrome"),
    "Chromium": ("Chromium", "org.chromium.Chromium"),
    "BraveSoftware/Brave-Browser": ("Brave Browser", "com.brave.Browser"),
}
_FRONT_GRACE_SECONDS = 1.0
_PAGE_CHANGING = frozenset(
    {"goto", "navigate", "click", "dblclick", "fill", "type", "press", "select", "check", "uncheck", "eval", "upload"}
)

# Role and accessible name, read the same way by inspect and by a click's check
# that the element is still the one inspect showed.
_ELEMENT_JS = """
const ROLES = {A: "link", BUTTON: "button", SELECT: "combobox", TEXTAREA: "textbox", FORM: "form",
  IMG: "img", NAV: "navigation", MAIN: "main", TABLE: "table", UL: "list", OL: "list", LI: "listitem",
  LABEL: "label", DIALOG: "dialog", H1: "heading", H2: "heading", H3: "heading", H4: "heading"};
const INPUTS = {checkbox: "checkbox", radio: "radio", submit: "button", button: "button", image: "button",
  file: "button", search: "searchbox", range: "slider"};
const roleOf = el => el.getAttribute("role")
  || (el.tagName === "INPUT" ? INPUTS[el.type] || "textbox" : ROLES[el.tagName]);
const nameOf = el => (el.getAttribute("aria-label") || (el.labels && el.labels[0] && el.labels[0].innerText)
  || (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) ? el.placeholder || el.name || "" : el.innerText)
  || el.getAttribute("title") || el.getAttribute("alt") || "").trim().replace(/\\s+/g, " ").slice(0, 200);
"""

# Reads the page into North's snapshot lines (uid=... role "name"), tagging each
# element so later calls can find it. Existing tags are kept, so a uid an agent
# or approval card saw keeps naming the same element. Plain text is uid=-.
_SNAPSHOT_SCRIPT = (
    _ELEMENT_JS
    + """
const limit = %(limit)d;
const shown = el => getComputedStyle(el).visibility !== "hidden" && el.getClientRects().length > 0;
let next = Number(document.documentElement.dataset.northNext || 0);
const lines = [];
const walk = (el, depth) => {
  if (lines.length >= limit || ["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(el.tagName)) return;
  // A hidden element is not listed, but its children still are: wrappers with
  // display: contents or visibility: hidden can hold visible content.
  if (el.type === "hidden" || getComputedStyle(el).display === "none") return;
  const role = shown(el) && roleOf(el);
  const interactive = shown(el) && (role || el.isContentEditable || el.hasAttribute("onclick") || el.tabIndex >= 0);
  if (interactive) {
    if (!el.dataset.northUid) el.dataset.northUid = "n" + (++next);
    lines.push("  ".repeat(depth) + "uid=" + el.dataset.northUid + " " + (role || el.tagName.toLowerCase())
      + " " + JSON.stringify(nameOf(el)));
  } else if (!el.children.length && shown(el) && el.innerText && el.innerText.trim()) {
    const words = el.innerText.trim().replace(/\\s+/g, " ").slice(0, 200);
    lines.push("  ".repeat(depth) + "uid=- text " + JSON.stringify(words));
  }
  for (const child of el.children) walk(child, depth + (interactive ? 1 : 0));
};
walk(document.body || document.documentElement, 0);
document.documentElement.dataset.northNext = String(next);
JSON.stringify({snapshot: lines.join("\\n"), url: location.href, title: document.title})
"""
)

# Where an element is on screen, after scrolling it into view, and what it is,
# so a click lands on the element the approval described or not at all.
_LOCATE_SCRIPT = (
    _ELEMENT_JS
    + """
const el = document.querySelector(%(selector)s);
let out = {found: false};
if (el && el.isConnected) {
  el.scrollIntoView({block: "center", inline: "center"});
  const r = el.getBoundingClientRect();
  const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
  const hit = document.elementFromPoint(x, y);
  out = {found: true, x, y, role: roleOf(el) || el.tagName.toLowerCase(), name: JSON.stringify(nameOf(el)),
    reachable: !!hit && (hit === el || el.contains(hit) || hit.contains(el))};
}
JSON.stringify(out)
"""
)

_TABLES_SCRIPT = """
const limit = %(limit)d, query = %(query)s.toLowerCase(), records = [];
for (const table of document.querySelectorAll("table")) {
  const headers = [...table.querySelectorAll("tr th")].map(c => c.innerText.trim().slice(0, 200));
  for (const row of table.querySelectorAll("tr")) {
    const cells = [...row.querySelectorAll("td")];
    if (!cells.length || (query && !row.innerText.toLowerCase().includes(query))) continue;
    records.push(Object.fromEntries(cells.map((c, i) =>
      [headers[i] || "column_" + (i + 1), c.innerText.trim().slice(0, 200)])));
    if (records.length >= limit) break;
  }
  if (records.length >= limit) break;
}
JSON.stringify(records)
"""

_ASSERT_SCRIPT = """
const kind = %(kind)s, condition = %(condition)s, expected = %(expected)s, selector = %(selector)s;
const el = selector ? document.querySelector(selector) : null;
let actual;
if (kind === "url") actual = location.href;
else if (kind === "exists") actual = el !== null;
else if (kind === "state") actual = !!el && ({checked: el.checked === true, unchecked: el.checked === false,
  selected: el.selected === true, enabled: !el.disabled, disabled: !!el.disabled,
  visible: el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden"})[condition] === true;
else if (el) actual = kind === "value" ? el.value : el.innerText;
else actual = document.body ? document.body.innerText : "";
const held = ["state", "exists"].includes(kind) ? actual === true
  : condition === "equals" ? String(actual) === expected
  : condition === "matches" ? new RegExp(expected).test(String(actual)) : String(actual).includes(expected);
JSON.stringify(held)
"""

_WAIT_SCRIPT = """
const condition = %(condition)s, pattern = %(pattern)s;
JSON.stringify(condition === "selector" ? !!document.querySelector(pattern)
  : condition === "url" ? location.href.includes(pattern)
  : condition === "text" ? !!document.body && document.body.innerText.includes(pattern)
  : condition === "loaded" ? location.href !== "about:blank" && document.readyState === "complete"
  : document.readyState === "complete")
"""


class BridgeInputError(ValueError):
    """The call was refused before anything reached the page."""


class _ToolError(RuntimeError):
    """TaskWindow reported that a tool failed. ``uncertain`` when it may still have acted."""

    def __init__(self, message: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def extension_id_for_key(key: str) -> str:
    """Chrome's extension ID for a manifest key: SHA-256 of the key, as letters a-p."""
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


NORTH_EXTENSION_ID = extension_id_for_key(_EXTENSION_KEY)


def profiles_loading(data_directory: Path, extension_id: str) -> dict[str, str]:
    """Profile directory -> folder, for each profile whose Chrome records *extension_id*."""
    found = {}
    for preferences in data_directory.glob("*/Secure Preferences"):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            settings = json.loads(preferences.read_text(encoding="utf-8")).get("extensions", {}).get("settings", {})
            entry = settings.get(extension_id) if isinstance(settings, dict) else None
            if isinstance(entry, dict) and entry.get("location") and entry.get("path"):
                found[preferences.parent.name] = str(Path(entry["path"]).resolve())
    return found


def _selector(params: dict[str, Any]) -> str:
    if params.get("uid"):
        uid = str(params["uid"])
        if not uid.removeprefix("n").isdigit():
            raise BridgeInputError("Unknown uid. Inspect the page again and use a uid it lists.")
        return f'[data-north-uid="{uid}"]'
    if params.get("selector"):
        return str(params["selector"])
    raise BridgeInputError(f"Action '{params.get('action')}' requires 'uid' or 'selector'.")


class TaskWindowBrowser:
    """Existing-profile adapter: same interface BrowserTool used for the old CDP bridge."""

    def __init__(self, north_home: Path) -> None:
        self._north_home = north_home
        self._daemons: dict[str, subprocess.Popen] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._install_lock = asyncio.Lock()
        # uid -> (role, name) last shown per task, to refuse clicks on a changed element.
        self._seen: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}

    # Pinned runtime ------------------------------------------------------

    async def _runtime(self) -> Path:
        bundle = resource_path("taskwindow")
        lock = await asyncio.to_thread((bundle / "package-lock.json").read_bytes)
        root = self._north_home / "runtime" / "taskwindow" / hashlib.sha256(lock).hexdigest()[:16]
        entry = root / "node_modules" / "taskwindow" / "src" / "index.js"
        async with self._install_lock:
            if not (root / "installed").is_file():
                npm = shutil.which("npm")
                if not npm:
                    raise RuntimeError("Existing browser profiles need Node 18+ and npm.")
                await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
                for name in ("package.json", "package-lock.json"):
                    await asyncio.to_thread(shutil.copyfile, bundle / name, root / name)
                await self._npm_ci(root, npm)
                await asyncio.to_thread(_apply_code_only_pairing, entry)
                await asyncio.to_thread((root / "installed").write_text, PINNED_VERSION, encoding="utf-8")
        source = await asyncio.to_thread(entry.read_text, encoding="utf-8")
        if _STORE_PAIRING in source or _CODE_ONLY_PAIRING not in source:
            raise RuntimeError("The TaskWindow daemon differs from the audited copy. Refusing to start it.")
        return entry

    @staticmethod
    async def _npm_ci(root: Path, npm: str) -> None:
        process = await asyncio.create_subprocess_exec(
            npm, "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund", cwd=root,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            await asyncio.wait_for(process.wait(), _INSTALL_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Could not install the pinned TaskWindow daemon. Check npm/network and retry.")

    # Per-profile daemon --------------------------------------------------

    def state_dir(self, profile: BrowserProfile) -> Path:
        return self._north_home / "browser" / "taskwindow" / profile.id

    def extension_dir(self, profile: BrowserProfile) -> Path:
        return (self.state_dir(profile) / "extension").resolve()

    def _port(self, profile: BrowserProfile) -> int:
        path = self.state_dir(profile) / "port"
        with contextlib.suppress(OSError, ValueError):
            return int(path.read_text(encoding="utf-8"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(port), encoding="utf-8")
        return port

    def _token(self, profile: BrowserProfile) -> str:
        return (self.state_dir(profile) / "daemon" / "token").read_text(encoding="utf-8").strip()

    async def _health(self, port: int) -> dict:
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                return response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def _ensure_daemon(self, profile: BrowserProfile) -> tuple[int, dict]:
        """Start this profile's daemon if it is not already answering with our key."""
        lock = self._locks.setdefault(profile.id, asyncio.Lock())
        async with lock:
            port = await asyncio.to_thread(self._port, profile)
            health = await self._health(port)
            if health.get("ok"):
                if health.get("version") != PINNED_VERSION or not await self._owns(profile, port):
                    raise RuntimeError(f"Port {port} answers, but not with {profile.name}'s pinned bridge.")
                return port, health
            entry = await self._runtime()
            node = shutil.which("node")
            if not node:
                raise RuntimeError("Existing browser profiles need Node 18+.")
            daemon_dir = self.state_dir(profile) / "daemon"
            await asyncio.to_thread(daemon_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread((daemon_dir / "no-update-check").touch)
            log = await asyncio.to_thread((self.state_dir(profile) / "daemon.log").open, "ab")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "TASKWINDOW_PORT": str(port),
                "TASKWINDOW_DIR": str(daemon_dir),
                "TASKWINDOW_NO_UPDATE_CHECK": "1",
            }
            self._daemons[profile.id] = subprocess.Popen(  # noqa: S603 - pinned, audited entry point
                [node, str(entry), "serve"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL
            )
            log.close()
            for _ in range(50):
                health = await self._health(port)
                if health.get("ok"):
                    return port, health
                await asyncio.sleep(0.1)
            raise RuntimeError(f"{profile.name}'s browser bridge did not start. See {daemon_dir.parent}/daemon.log.")

    async def _owns(self, profile: BrowserProfile, port: int) -> bool:
        """Whether the daemon on *port* accepts this profile's key (an orphan from an earlier run).

        Asks for the tool list, never a pairing code: issuing a code would void
        the one waiting in the extension folder.
        """
        daemon = self._daemons.get(profile.id)
        if daemon is not None and daemon.poll() is None:
            return True
        with contextlib.suppress(OSError, httpx.HTTPError):
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        "Authorization": f"Bearer {self._token(profile)}",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                return response.status_code == 200
        return False

    # Setup and verification ---------------------------------------------

    async def setup(self, profile: BrowserProfile) -> dict:
        """Prepare this profile's extension folder with a fresh one-time pairing code."""
        port, _ = await self._ensure_daemon(profile)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/pair/request", headers={"Authorization": f"Bearer {self._token(profile)}"}
            )
            response.raise_for_status()
            code = response.json()["code"]
        folder = self.extension_dir(profile)
        await asyncio.to_thread(_write_extension, folder, {"code": code, "port": port})
        return {
            "folder": str(folder),
            "extension_id": NORTH_EXTENSION_ID,
            "steps": [
                f"Open Chrome with the {profile.name} profile and go to chrome://extensions.",
                "Turn on Developer mode, top right.",
                "If TaskWindow from the Chrome Web Store is installed in this profile, remove it.",
                "Click Load unpacked and choose the folder below. It pairs by itself within 10 minutes.",
            ],
        }

    async def verify(self, profile: BrowserProfile, *, wait: bool = True) -> dict:
        """Prove the extension answering this profile's daemon lives in this profile."""
        port, health = await self._ensure_daemon(profile)
        if wait and not health.get("extensionConnected"):
            health = await self._await_connection(profile, port)
        if not health.get("extensionConnected"):
            raise BridgeInputError(
                f"{profile.name} is not connected. Open Browser profiles in North's dashboard and connect it."
            )
        if health.get("extensionId") != NORTH_EXTENSION_ID:
            raise BridgeInputError(f"{profile.name}'s bridge is paired with a different extension. Connect it again.")
        # The folder holds this daemon's one-time pairing code, so the profile
        # that loaded this folder, and only it, is the one this daemon drives.
        folder = str(self.extension_dir(profile))
        loading = await asyncio.to_thread(profiles_loading, Path(profile.data_directory), NORTH_EXTENSION_ID)
        holders = sorted(directory for directory, path in loading.items() if path == folder)
        if holders != [profile.profile_directory]:
            raise BridgeInputError(
                f"North's folder for {profile.name} is loaded in {holders or 'no profile'}, "
                f"not only in {profile.profile_directory}. Load it in that profile only and connect again."
            )
        return {
            "profile_verified": True,
            "login_verified": False,
            "connection_id": hashlib.sha256(f"{port}:{folder}".encode()).hexdigest(),
            "extension_version": health.get("extensionVersion"),
        }

    async def _await_connection(self, profile: BrowserProfile, port: int) -> dict:
        """Give a running profile time to reconnect, then open it in the background."""
        for _ in range(_CONNECT_GRACE_SECONDS):
            await asyncio.sleep(1)
            health = await self._health(port)
            if health.get("extensionConnected"):
                return health
        async with _front_kept(profile):
            await asyncio.to_thread(_open_profile_in_background, profile)
        for _ in range(_LAUNCH_WAIT_SECONDS):
            await asyncio.sleep(1)
            health = await self._health(port)
            if health.get("extensionConnected"):
                return health
        return health

    # Tool calls -----------------------------------------------------------

    async def _tool(self, profile: BrowserProfile, name: str, arguments: dict, timeout: float = 60) -> dict:
        port = await asyncio.to_thread(self._port, profile)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        try:
            async with httpx.AsyncClient(timeout=timeout + 10, trust_env=False) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token(profile)}",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                response.raise_for_status()
                result = response.json()["result"]
        except (httpx.HTTPError, KeyError, ValueError, OSError) as exc:
            raise _ToolError(f"The browser bridge did not answer ({type(exc).__name__}).", uncertain=True) from exc
        if result.get("isError"):
            text = " ".join(block.get("text", "") for block in result.get("content", []))[:500]
            uncertain = any(word in text.lower() for word in ("timed out", "timeout", "disconnected"))
            raise _ToolError(text or f"{name} failed.", uncertain=uncertain)
        return result

    @staticmethod
    def _text(result: dict) -> str:
        return next((block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"), "")

    def _session(self, profile: BrowserProfile, task_id: str) -> str:
        """A secret per task, derived from the daemon key: a restarted North resumes the same tabs."""
        return hashlib.sha256(f"{self._token(profile)}:{task_id}".encode()).hexdigest()[:40]

    async def _script(self, profile: BrowserProfile, session: str, code: str) -> Any:
        """Run one of North's own page reads. Only these are retried: they change nothing."""
        for attempt in range(_READ_ATTEMPTS):
            try:
                result = await self._tool(profile, "javascript_execute", {"code": code, "sessionToken": session})
                return json.loads(self._text(result))
            except _ToolError as exc:
                if attempt == _READ_ATTEMPTS - 1 or not any(sign in str(exc) for sign in _NAVIGATION_RACES):
                    raise
                await asyncio.sleep(0.5)
        raise AssertionError("unreachable")

    async def call(self, profile: BrowserProfile, params: dict) -> ToolOutput:
        """Run one approved action. BrowserTool has already applied policy and the profile check."""
        task_id = str(params.get("task_id") or "")
        if not task_id:
            return ToolOutput(success=False, error="An existing-browser call requires a task_id.")
        action = str(params.get("action") or "")
        try:
            identity = await self.verify(profile)
            if action in {"preflight", "status"}:
                return ToolOutput(
                    success=True,
                    data={"action": action, **identity, "uncertain_action": self._in_flight(profile, task_id)},
                )
            if action == "close":
                await self._close(profile, task_id)
                return ToolOutput(success=True, data={"action": "close", "closed": True, **identity})
            changes_page = action in _PAGE_CHANGING or (action == "read" and bool(params.get("url")))
            if changes_page and self._in_flight(profile, task_id):
                raise BridgeInputError(
                    "An earlier browser action has an unknown outcome. "
                    "Inspect and ask the user to review; do not replay it."
                )
            if changes_page:
                self._mark(profile, task_id, action)
            try:
                data = await self._act(profile, task_id, action, params)
            except (BridgeInputError, _ToolError) as exc:
                if changes_page and not getattr(exc, "uncertain", False):
                    self._mark(profile, task_id, None)
                raise
            if changes_page:
                self._mark(profile, task_id, None)
            if data.get("held") is False:
                return ToolOutput(
                    success=False, data={"action": action, **identity, **data}, error="Browser assertion was not met."
                )
            return ToolOutput(success=True, data={"action": action, **identity, **data})
        except (BridgeInputError, _ToolError, RuntimeError, OSError, httpx.HTTPError) as exc:
            return ToolOutput(success=False, error=str(exc))

    async def _act(self, profile: BrowserProfile, task_id: str, action: str, params: dict) -> dict:
        session = self._session(profile, task_id)
        if action in {"goto", "navigate"}:
            return await self._open(profile, session, params)
        if action == "read":
            if params.get("url"):
                await self._open(profile, session, params)
            text = self._text(await self._tool(profile, "get_page_text", {"sessionToken": session}))
            page = await self._script(profile, session, "JSON.stringify({title: document.title, url: location.href})")
            return {"article": {"title": page["title"], "content": text}, "url": page["url"]}
        if action == "inspect":
            return await self._inspect(profile, task_id, session, params)
        if action == "extract":
            limit = min(max(int(params.get("limit") or 25), 1), 100)
            code = _TABLES_SCRIPT % {"limit": limit, "query": json.dumps(str(params.get("query") or ""))}
            items = await self._script(profile, session, code)
            return {
                "items": items,
                "count": len(items),
                "source": "html_tables",
                "note": "For other listings, use read or inspect.",
            }
        if action in {"click", "dblclick"}:
            return await self._click(profile, task_id, session, params, double=action == "dblclick")
        if action in {"fill", "select", "check", "uncheck"}:
            value = {"check": True, "uncheck": False}.get(action, str(params.get("value", "")))
            await self._tool(
                profile, "form_input", {"selector": _selector(params), "value": value, "sessionToken": session}
            )
            return {}
        if action == "type":
            if params.get("uid") or params.get("selector"):
                await self._script(
                    profile, session, f"document.querySelector({json.dumps(_selector(params))})?.focus(); 'null'"
                )
            await self._tool(
                profile, "computer", {"action": "type", "text": str(params.get("value", "")), "sessionToken": session}
            )
            return {}
        if action == "press":
            await self._tool(
                profile,
                "computer",
                {"action": "key", "key": str(params.get("value") or "Enter"), "sessionToken": session},
            )
            return {}
        if action == "upload":
            return await self._upload(profile, session, params)
        if action == "eval":
            js = str(params.get("js") or "").strip()
            if not js:
                raise BridgeInputError("Parameter 'js' is required for action='eval'.")
            result = await self._tool(profile, "javascript_execute", {"code": js, "sessionToken": session})
            text = self._text(result)
            with contextlib.suppress(ValueError):
                return {"result": json.loads(text)}
            return {"result": text}
        if action == "assert":
            code = _ASSERT_SCRIPT % {
                "kind": json.dumps(str(params.get("assert_type") or "text")),
                "condition": json.dumps(str(params.get("assert_condition") or "contains")),
                "expected": json.dumps(str(params.get("value") or "")),
                "selector": json.dumps(_selector(params) if params.get("uid") or params.get("selector") else ""),
            }
            return {
                "held": await self._script(profile, session, code),
                "assert_type": params.get("assert_type") or "text",
            }
        if action == "wait":
            return await self._wait(profile, session, params)
        if action == "screenshot":
            return await self._screenshot(profile, session, params)
        raise BridgeInputError(f"Action '{action}' is not available for existing browser profiles.")

    async def _open(self, profile: BrowserProfile, session: str, params: dict) -> dict:
        url = str(params.get("url") or "").strip()
        if not url:
            raise BridgeInputError("A URL is required.")
        listing = self._text(await self._tool(profile, "tabs_list", {"sessionToken": session}))
        if listing.startswith("no tabs"):
            arguments = {"url": url, "task": _TASK_GROUP, "longRunning": True, "sessionToken": session}
            async with _front_kept(profile):  # the first tab may create North's window
                await self._tool(profile, "tabs_create", arguments)
            with contextlib.suppress(BridgeInputError):  # a slow page is still open, as with navigate
                await self._wait(profile, session, {"condition": "loaded", "timeout_seconds": 15})
        else:
            await self._tool(profile, "navigate", {"url": url, "sessionToken": session})
        return await self._script(profile, session, "JSON.stringify({url: location.href, title: document.title})")

    async def _inspect(self, profile: BrowserProfile, task_id: str, session: str, params: dict) -> dict:
        limit = min(max(int(params.get("limit") or 100), 1), 1000)
        page = await self._script(profile, session, _SNAPSHOT_SCRIPT % {"limit": limit})
        seen = self._seen.setdefault((profile.id, task_id), {})
        for line in page["snapshot"].splitlines():
            uid, _, rest = line.strip().removeprefix("uid=").partition(" ")
            role, _, name = rest.partition(" ")
            seen[uid] = (role, name)
        return {"snapshot": page["snapshot"], "tree": page["snapshot"], "url": page["url"], "title": page["title"]}

    async def _click(self, profile: BrowserProfile, task_id: str, session: str, params: dict, *, double: bool) -> dict:
        if params.get("xy") and not (params.get("uid") or params.get("selector")):
            try:
                x, y = (int(float(part)) for part in str(params["xy"]).split(","))
            except ValueError:
                raise BridgeInputError("Invalid coordinates.") from None
        else:
            spot = await self._script(profile, session, _LOCATE_SCRIPT % {"selector": json.dumps(_selector(params))})
            if not spot.get("found"):
                raise BridgeInputError("Element was not found or was removed; inspect again.")
            if not spot.get("reachable"):
                raise BridgeInputError("Another element covers this one; inspect again before clicking.")
            uid = str(params.get("uid") or "")
            if uid and self._seen.get((profile.id, task_id), {}).get(uid) != (spot["role"], spot["name"]):
                raise BridgeInputError("This element changed since it was inspected. Inspect again before clicking.")
            x, y = spot["x"], spot["y"]
        kind = "double_click" if double else "left_click"
        await self._tool(profile, "computer", {"action": kind, "x": x, "y": y, "sessionToken": session})
        return {"x": x, "y": y}

    async def _upload(self, profile: BrowserProfile, session: str, params: dict) -> dict:
        path = Path(str(params.get("filename") or "")).expanduser()
        if not path.is_file():
            raise BridgeInputError("Upload needs 'filename': an existing local file.")
        data = await asyncio.to_thread(path.read_bytes)
        file = {
            "name": path.name,
            "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "data": base64.b64encode(data).decode(),
        }
        await self._tool(
            profile, "file_upload", {"files": [file], "selector": _selector(params), "sessionToken": session}
        )
        return {"uploaded": path.name, "bytes": len(data)}

    async def _wait(self, profile: BrowserProfile, session: str, params: dict) -> dict:
        condition = str(params.get("condition") or "network-idle")
        pattern = str(params.get("pattern") or "")
        if condition in {"text", "url", "selector"} and not pattern:
            raise BridgeInputError("Text, URL and selector waits require pattern.")
        code = _WAIT_SCRIPT % {"condition": json.dumps(condition), "pattern": json.dumps(pattern)}
        deadline = asyncio.get_running_loop().time() + min(max(int(params.get("timeout_seconds") or 30), 1), 120)
        while asyncio.get_running_loop().time() < deadline:
            if await self._script(profile, session, code):
                return {"condition_met": True}
            await asyncio.sleep(0.5)
        raise BridgeInputError(f"Timed out waiting for {condition}.")

    async def _screenshot(self, profile: BrowserProfile, session: str, params: dict) -> dict:
        filename = str(params.get("filename") or "")
        if not filename:
            raise BridgeInputError("A workspace filename is required.")
        result = await self._tool(profile, "computer", {"action": "screenshot", "sessionToken": session})
        image = next((block for block in result.get("content", []) if block.get("type") == "image"), None)
        if image is None:
            raise BridgeInputError("The browser returned no image.")
        await asyncio.to_thread(Path(filename).write_bytes, base64.b64decode(image["data"]))
        return {"filename": filename}

    async def _close(self, profile: BrowserProfile, task_id: str) -> None:
        session = self._session(profile, task_id)
        listing = (await self._tool(profile, "tabs_list", {"sessionToken": session})).get("content", [])
        for line in (block.get("text", "") for block in listing):
            for row in line.splitlines():
                tab_id = row.split(".", 1)[0]
                if tab_id.isdigit():
                    await self._tool(profile, "tabs_close", {"tabId": int(tab_id), "sessionToken": session})
        self._mark(profile, task_id, None)
        self._seen.pop((profile.id, task_id), None)

    # Unknown-outcome markers ---------------------------------------------

    def _marker(self, profile: BrowserProfile, task_id: str) -> Path:
        return self.state_dir(profile) / "tasks" / f"{hashlib.sha256(task_id.encode()).hexdigest()[:24]}.json"

    def _in_flight(self, profile: BrowserProfile, task_id: str) -> bool:
        return self._marker(profile, task_id).is_file()

    def _mark(self, profile: BrowserProfile, task_id: str, action: str | None) -> None:
        """Record a page action before it runs; it stays if North cannot tell whether it happened."""
        path = self._marker(profile, task_id)
        if action is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"action": action}), encoding="utf-8")

    # Approval cards and shutdown -----------------------------------------

    def description_runner(self, profile: BrowserProfile, params: dict):
        """Serve the approval card's two page reads: its fixed script and an inspect."""
        session = self._session(profile, str(params.get("task_id") or "default"))

        async def read_page(command: list[str], timeout: int):
            action, *arguments = command
            if action == "eval":
                result = await self._tool(
                    profile, "javascript_execute", {"code": arguments[0], "sessionToken": session}, timeout
                )
                return SimpleNamespace(stdout=json.dumps({"result": self._text(result)}))
            if action == "inspect":
                page = await self._script(profile, session, _SNAPSHOT_SCRIPT % {"limit": 1000})
                return SimpleNamespace(stdout=json.dumps({"snapshot": page["snapshot"]}))
            raise RuntimeError("Unsupported page read.")

        return read_page

    async def aclose(self) -> None:
        """Stop North's daemons. Chrome, its windows and task drafts stay open."""
        for daemon in self._daemons.values():
            with contextlib.suppress(OSError):
                daemon.terminate()
        self._daemons.clear()


def _apply_code_only_pairing(entry: Path) -> None:
    source = entry.read_text(encoding="utf-8")
    if source.count(_STORE_PAIRING) != 1:
        raise RuntimeError("The TaskWindow daemon differs from the audited copy. Refusing to patch it.")
    entry.write_text(source.replace(_STORE_PAIRING, _CODE_ONLY_PAIRING), encoding="utf-8")


def _write_extension(folder: Path, bootstrap: dict) -> None:
    """A per-profile copy of the audited extension: its path fixes the extension ID.

    A copy that still matches is kept, so Chrome's loaded extension is never
    pulled out from under it; only the pairing code is refreshed.
    """
    audited = resource_path("taskwindow/extension")
    if folder.exists() and not _same_files(audited, folder):
        shutil.rmtree(folder)
    if not folder.exists():
        shutil.copytree(audited, folder)
        manifest = json.loads((audited / "manifest.json").read_text(encoding="utf-8"))
        (folder / "manifest.json").write_text(
            json.dumps({**manifest, "key": _EXTENSION_KEY}, indent=2), encoding="utf-8"
        )
    (folder / "taskwindow-bootstrap.json").write_text(json.dumps(bootstrap), encoding="utf-8")
    (folder / "taskwindow-bootstrap.json").chmod(0o600)


def _same_files(audited: Path, folder: Path) -> bool:
    """Whether *folder* is the audited extension plus North's manifest key, nothing changed."""
    for source in audited.rglob("*"):
        copy = folder / source.relative_to(audited)
        if source.name == "manifest.json" and source.parent == audited:
            with contextlib.suppress(OSError, ValueError):
                if json.loads(copy.read_text(encoding="utf-8")) == {
                    **json.loads(source.read_text(encoding="utf-8")),
                    "key": _EXTENSION_KEY,
                }:
                    continue
            return False
        if source.is_file() and (not copy.is_file() or copy.read_bytes() != source.read_bytes()):
            return False
    return True


def _browser_app(profile: BrowserProfile) -> tuple[str, str] | None:
    return next((app for part, app in _BROWSER_APPS.items() if profile.data_directory.endswith(part)), None)


@contextlib.asynccontextmanager
async def _front_kept(profile: BrowserProfile):
    """Hand the front back if Chrome comes forward while North opens its window.

    Chrome creates North's window unfocused, yet on macOS can still raise itself;
    TaskWindow hands focus back between Chrome windows but cannot hand it back to
    another app. Only a jump to this browser, during the call or just after, is
    undone, and only once, so a person who switches to Chrome is not fought.
    """
    before = await asyncio.to_thread(frontmost_app)
    app = _browser_app(profile)
    if before is None or app is None or before == app[1]:
        yield
        return

    async def watch() -> None:
        while await asyncio.to_thread(frontmost_app) != app[1]:
            await asyncio.sleep(0.1)
        logger.info("%s came forward while opening North's window; handing the front back to %s", app[0], before)
        await asyncio.to_thread(activate, before)

    watcher = asyncio.create_task(watch())
    try:
        yield
        await asyncio.wait({watcher}, timeout=_FRONT_GRACE_SECONDS)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def _open_profile_in_background(profile: BrowserProfile) -> None:
    """Open the profile without bringing Chrome forward, so its extension can connect."""
    if sys.platform != "darwin":
        return
    app = (_browser_app(profile) or (None,))[0]
    if app:
        subprocess.run(  # noqa: S603 - fixed app name and profile directory from settings
            ["/usr/bin/open", "-g", "-a", app, "--args", f"--profile-directory={profile.profile_directory}"],
            check=False, capture_output=True, timeout=15,
        )  # fmt: skip
