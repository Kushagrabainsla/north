"""Small real-browser probe: named sessions, cookie isolation, reopen, CDP identity.

Only synthetic cookies on a loopback server are used. Unique managed browsers
are closed and purged in finally; no existing Chrome profile is accessed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import aiohttp

from approval.approvals import Approvals
from approval.policy import ApprovalPolicy
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tools.models import ToolInput
from tools.universal.browser import BrowserTool


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        if query.get("login"):
            self.send_header("Set-Cookie", f"account={query['login'][0]}; Path=/; Max-Age=3600; SameSite=Lax")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>Synthetic login</title><body>Browser profile test</body></html>")

    def log_message(self, *_args):
        pass


def main():
    prefix = f"north-probe-{uuid.uuid4().hex[:12]}"
    names = [f"{prefix}-personal", f"{prefix}-university"]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    results = []

    def run(name, *args):
        process = subprocess.run(
            ["chrome-agent", "--json", "--browser", name, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(process.stdout)

    def value(name, script, *flags):
        return run(name, *flags, "eval", script)["result"]

    def endpoint_for(name):
        root = Path.home() / ".chrome-agent" / "browsers" / name / "chromium-profile"
        port, websocket = (root / "DevToolsActivePort").read_text().splitlines()[:2]
        return f"ws://127.0.0.1:{port}{websocket}"

    async def graceful_close(name):
        async with aiohttp.ClientSession() as session, session.ws_connect(endpoint_for(name)) as socket:
            await socket.send_json({"id": 1, "method": "Browser.close"})
            await socket.receive(timeout=5)

    try:
        for name in names:
            run(name, "goto", url)
        for name, account in zip(names, ("personal", "university"), strict=True):
            run(name, "goto", f"{url}/?login={account}")
        for repeat in range(3):
            for name, account in zip(names, ("personal", "university"), strict=True):
                run(name, "goto", url)
                assert value(name, "document.cookie") == f"account={account}"
            results.append({"reuse_round": repeat + 1, "separate_logins_preserved": True})
        for round_number in (1, 2):
            for name, account in zip(names, ("personal", "university"), strict=True):
                cookie = value(name, "document.cookie")
                assert cookie == f"account={account}", cookie
                if round_number == 1:
                    run(name, "close")
                else:
                    asyncio.run(graceful_close(name))
                    run(name, "close")
                time.sleep(1)
                run(name, "goto", url)
                survives = value(name, "document.cookie") == f"account={account}"
                results.append(
                    {
                        "close": "vendor" if round_number == 1 else "graceful",
                        "profile": account,
                        "isolated_login": True,
                        "reopen_preserves_login": survives,
                    }
                )
                run(name, "goto", f"{url}/?login={account}")
        name = names[1]
        profile_root = Path.home() / ".chrome-agent" / "browsers" / name / "chromium-profile"
        port, websocket = (profile_root / "DevToolsActivePort").read_text().splitlines()[:2]
        endpoint = f"ws://127.0.0.1:{port}{websocket}"
        run(name, "--connect", endpoint, "--page", "identity", "goto", "chrome://version")
        actual = value(
            name, 'document.getElementById("profile_path").textContent', "--connect", endpoint, "--page", "identity"
        )
        assert Path(actual).resolve() == (profile_root / "Default").resolve(), actual
        assert Path(actual).resolve() != (profile_root / "Profile 1").resolve()
        results.append({"cdp_profile_identity": True, "wrong_profile_detectable": True})

        async def check_north(settings):
            tool = BrowserTool(binary_cmd=["chrome-agent"], north_settings=settings)
            tool.approvals = Approvals(ApprovalPolicy(mode_provider=lambda: ApprovalMode.YOLO), None)
            profile_id = names[1].removeprefix("north-")
            out = await tool.execute(
                ToolInput(
                    params={"action": "preflight", "profile_id": profile_id, "task_id": "synthetic-profile-probe"}
                )
            )
            assert out.success and out.data["profile_verified"], out.error
            assert not out.data["login_verified"]
            item = settings.browser_profiles[0]
            settings.set_browser_profiles([item.model_copy(update={"profile_directory": "Profile 1"})])
            wrong = await tool.execute(
                ToolInput(
                    params={"action": "preflight", "profile_id": profile_id, "task_id": "synthetic-profile-probe"}
                )
            )
            assert not wrong.success and "different profile" in wrong.error, wrong
            return {"north_native_connection_verified": True, "north_wrong_profile_rejected": True}

        with TemporaryDirectory(prefix="north-browser-probe-") as scratch:
            settings = NorthSettings(Path(scratch) / "settings.json")
            settings.set_browser_profiles(
                [
                    BrowserProfile(
                        id=names[1].removeprefix("north-"),
                        name="Synthetic University",
                        purpose="Synthetic experiment only",
                        context="existing",
                        data_directory=str(profile_root),
                    )
                ]
            )
            results.append(asyncio.run(check_north(settings)))
        print(
            json.dumps(
                {
                    "completed": True,
                    "reopen_is_reliable": all(item.get("reopen_preserves_login", True) for item in results),
                    "checks": results,
                },
                indent=2,
            )
        )
    finally:
        for name in names:
            with contextlib.suppress(subprocess.SubprocessError, json.JSONDecodeError):
                run(name, "close", "--purge")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
