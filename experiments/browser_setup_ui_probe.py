"""Built-dashboard smoke test against synthetic APIs in a disposable browser.

No North daemon, real provider, user settings, personal profile or site is used.
Run: .venv/bin/python -m experiments.browser_setup_ui_probe
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import mimetypes
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, help="Optional directory for synthetic UI screenshots")
    options = parser.parse_args()
    if options.screenshots:
        options.screenshots.mkdir(parents=True, exist_ok=True)
    dist = Path(__file__).resolve().parents[1] / "web/dist"
    profile_name = f"north-ui-probe-{uuid.uuid4().hex[:12]}"
    settings = {
        "timezone": "UTC",
        "timezone_configured": False,
        "autonomy": "ask",
        "autonomy_options": [{"value": "ask", "description": "Ask before actions"}],
        "routing": "auto",
        "power": "cruise",
        "timezone_options": ["UTC"],
    }
    setup = {
        "status": "not_started",
        "step": 0,
        "browser": {"state": "available", "detail": "Synthetic connection"},
        "coding_agents": {"codex": True, "claude": True},
        "providers": [{"id": "test", "name": "Synthetic provider", "configured": True, "optional": False}],
    }
    profiles = {"profiles": [], "tests": {}}

    class Page(BaseHTTPRequestHandler):
        def reply(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/web/api/setup":
                return self.reply({**setup, **settings, "profiles": profiles["profiles"]})
            if path == "/orchestrator/settings":
                return self.reply(settings)
            if path == "/web/api/browser/profiles":
                return self.reply(profiles)
            if path == "/health":
                return self.reply({"status": "ok", "checks": {}})
            relative = path.removeprefix("/app/").lstrip("/")
            target = (dist / relative).resolve() if relative else dist / "index.html"
            if not target.is_relative_to(dist) or not target.is_file():
                self.send_error(404)
                return None
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return None

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/web/session":
                return self.reply({"csrf": "synthetic"})
            if self.path == "/web/api/setup":
                setup.update(body)
                return self.reply({**setup, **settings})
            if self.path == "/orchestrator/settings":
                if not settings["timezone_configured"]:
                    settings.update(timezone=body["timezone"], timezone_configured=True)
                return self.reply(settings)
            if self.path == "/web/api/browser/profiles":
                profiles.update(profiles=body["profiles"], tests={})
                return self.reply(profiles)
            if self.path == "/web/api/browser/profiles/discover":
                return self.reply(
                    {
                        "profiles": [
                            {
                                "id": "synthetic-student",
                                "name": "Student",
                                "purpose": "",
                                "browser": "Chrome",
                                "context": "existing",
                                "data_directory": "/tmp/north-ui-synthetic/Chrome",
                                "profile_directory": "Profile 1",
                                "connect": "",
                                "headed": True,
                                "enabled": False,
                            }
                        ]
                    }
                )
            if self.path.endswith("/test"):
                profile_id = self.path.split("/")[-2]
                profiles["tests"][profile_id] = {"status": "completed", "data": {"profile_verified": True}}
                return self.reply(profiles["tests"][profile_id])
            self.send_error(404)
            return None

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(*args):
        process = subprocess.run(
            ["chrome-agent", "--json", "--browser", profile_name, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(process.stdout)

    def click(selector):
        result = run("click", "--selector", selector)
        assert result.get("ok") and result.get("delivery") != "intercepted", result

    def evaluate(script):
        return run("eval", script)["result"]

    async def check_layout(width, label):
        # Only this probe's disposable Chrome; never attach to a user browser.
        root = Path.home() / ".chrome-agent" / "browsers" / profile_name / "chromium-profile"
        port = (root / "DevToolsActivePort").read_text().splitlines()[0]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/json/list") as response:
                targets = await response.json()
            target = next(t for t in targets if t.get("url", "").startswith(f"http://127.0.0.1:{server.server_port}/"))
            async with session.ws_connect(target["webSocketDebuggerUrl"]) as socket:
                command_id = 0

                async def command(method, params):
                    nonlocal command_id
                    command_id += 1
                    await socket.send_json({"id": command_id, "method": method, "params": params})
                    while True:
                        message = await socket.receive_json(timeout=10)
                        if message.get("id") == command_id:
                            assert "error" not in message, message
                            return message.get("result", {})

                await command(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": 1600, "deviceScaleFactor": 1, "mobile": False},
                )
                # Keep the CDP session open: closing it clears its emulation.
                await command(
                    "Runtime.evaluate",
                    {
                        "expression": "document.fonts.ready.then(() => new Promise(requestAnimationFrame))",
                        "awaitPromise": True,
                    },
                )
                measured = await command(
                    "Runtime.evaluate",
                    {
                        "returnByValue": True,
                        "expression": """(() => {
              const page = document.querySelector('.page');
              const bounds = page.getBoundingClientRect();
              const buttons = [...page.querySelectorAll('button')];
              return {
                width: innerWidth,
                pageFits: bounds.right <= innerWidth + 1 && bounds.left >= 0,
                noOverflow: page.scrollWidth <= page.clientWidth + 1,
                styledButtons: buttons.every(b => b.matches('.ghost-button, .primary-button, .segmented button')),
                controlSizes: buttons.every(b => parseFloat(getComputedStyle(b).fontSize) <= 11
                  && parseFloat(getComputedStyle(b).minHeight) >= 34),
                formFits: [...page.querySelectorAll('.setup-form input, .setup-form textarea')]
                  .every(e => e.getBoundingClientRect().right <= bounds.right),
              };
            })()""",
                    },
                )
                result = measured["result"]["value"]
                assert result["width"] == width, result
                assert all(
                    result[key] for key in ("pageFits", "noOverflow", "styledButtons", "controlSizes", "formFits")
                ), result
                if options.screenshots:
                    await command(
                        "Runtime.evaluate",
                        {"expression": "window.scrollTo(0, 0); document.querySelector('.workspace').scrollTop = 0"},
                    )
                    if label.startswith("existing-profile"):
                        await command(
                            "Runtime.evaluate", {"expression": "document.querySelector('.setup-form').scrollIntoView()"}
                        )
                    capture = await command("Page.captureScreenshot", {"format": "png"})
                    destination = options.screenshots / f"{label}.png"
                    destination.write_bytes(base64.b64decode(capture["data"]))
                return result

    try:
        run("goto", f"http://127.0.0.1:{server.server_port}/app/#/setup")
        run("wait", "text", "Connect an AI provider")
        click(".setup-steps button:nth-child(3)")
        run("wait", "text", "Add North-managed profile")
        click(".browser-profile-toolbar button")
        run("wait", "selector", ".setup-form")
        run("fill", "--selector", ".setup-form input:not([type=checkbox])", "University")
        run("fill", "--selector", ".setup-form textarea", "University work and job applications")
        layouts = {}
        for width in (1440, 820, 390):
            layouts[f"setup-form-{width}"] = asyncio.run(check_layout(width, f"setup-form-{width}"))
        click(".setup-form button[type=submit]")
        run("wait", "text", "University work and job applications")
        click(".browser-profile .setup-actions button")
        run("wait", "text", "Profile verified · login not checked")
        assert len(profiles["profiles"]) == 1
        assert profiles["profiles"][0]["purpose"] == "University work and job applications"
        assert settings["timezone_configured"]
        # A reload must retain progress and the server-backed profile.
        run("goto", f"http://127.0.0.1:{server.server_port}/app/?reload=1#/setup")
        run("wait", "text", "University work and job applications")
        assert setup["step"] == 2
        click(".browser-profile .setup-actions button:last-child")
        run("wait", "text", "University · disabled")
        assert not profiles["profiles"][0]["enabled"]
        # The existing Settings dials keep their order. Browser profiles is last
        # and spans both desktop columns instead of squeezing them into one.
        click('.header-actions a[href="#/settings"]')
        run("wait", "text", "Model routing")
        headings = evaluate(
            "[...document.querySelectorAll('.settings-grid > .panel > header h2')].map(e => e.textContent)"
        )
        assert headings == ["Model routing", "Power", "Autonomy", "Time zone", "Browser profiles"], headings
        for width in (1440, 820, 390):
            layouts[f"settings-{width}"] = asyncio.run(check_layout(width, f"settings-{width}"))
            spans_grid = evaluate(
                """(() => {
                  const grid = document.querySelector('.settings-grid').getBoundingClientRect();
                  const profile = document.querySelector('.browser-profiles').getBoundingClientRect();
                  return Math.abs(grid.width - profile.width) < 1;
                })()"""
            )
            assert spans_grid
        # Discovery and permissions render against synthetic profile data only.
        click(".browser-profile-toolbar button:nth-child(2)")
        run("wait", "text", "Chrome · Student")
        click(".setup-discovery button")
        run("wait", "text", "Connection details")
        click(".setup-connection-details summary")
        run("wait", "selector", ".setup-connection-details[open]")
        layouts["existing-profile-390"] = asyncio.run(check_layout(390, "existing-profile-390"))
        print(
            json.dumps(
                {
                    "passed": True,
                    "built_dashboard": True,
                    "profile_create_and_test": True,
                    "reload_resumes_setup": True,
                    "disconnect_disables_only": True,
                    "settings_profiles_last_and_full_width": True,
                    "responsive_layouts": layouts,
                }
            )
        )
    except Exception:
        with contextlib.suppress(subprocess.SubprocessError, ValueError):
            print(json.dumps({"synthetic_api_state": {"setup": setup, "settings": settings, "profiles": profiles}}))
            print(json.dumps({"page_at_failure": run("eval", "document.body.innerText")}))
            print(json.dumps({"browser_console": run("console")}))
        raise
    finally:
        with contextlib.suppress(subprocess.SubprocessError, ValueError):
            run("close", "--purge")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
