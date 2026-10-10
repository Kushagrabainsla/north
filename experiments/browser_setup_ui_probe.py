"""Built-dashboard smoke test against synthetic APIs in a disposable browser.

No North daemon, real provider, user settings, personal profile or site is used.
Run: .venv/bin/python -m experiments.browser_setup_ui_probe
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def main():
    dist = Path(__file__).resolve().parents[1] / "web/dist"
    profile_name = f"north-ui-probe-{uuid.uuid4().hex[:12]}"
    settings = {
        "timezone": "UTC",
        "timezone_configured": False,
        "autonomy": "ask",
        "autonomy_options": [{"value": "ask", "description": "Ask before actions"}],
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

    try:
        run("goto", f"http://127.0.0.1:{server.server_port}/app/#/setup")
        run("wait", "text", "Connect an AI provider")
        click(".setup-steps button:nth-child(3)")
        run("wait", "text", "Add North-managed profile")
        click(".setup-actions button")
        run("wait", "selector", ".setup-form")
        run("fill", "--selector", ".setup-form input:not([type=checkbox])", "University")
        run("fill", "--selector", ".setup-form textarea", "University work and job applications")
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
        print(
            json.dumps(
                {
                    "passed": True,
                    "built_dashboard": True,
                    "profile_create_and_test": True,
                    "reload_resumes_setup": True,
                    "disconnect_disables_only": True,
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
