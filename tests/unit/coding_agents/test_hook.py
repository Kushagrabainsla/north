"""The hook shim, run as a real subprocess against a throwaway local server."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import coding_agents.hook as hook_module

HOOK = Path(hook_module.__file__)
PAYLOAD = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "make test"}}
ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}


class _Server:
    def __init__(self, status: int = 200, body: object = ALLOW, delay: float = 0.0) -> None:
        self.seen: list[tuple[str | None, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                outer.seen.append((self.headers.get("X-Gate-Token"), json.loads(self.rfile.read(length))))
                time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/gate"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def serve():
    servers: list[_Server] = []

    def start(**kwargs) -> _Server:
        servers.append(_Server(**kwargs))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def _run(url: str | None, token: str | None = "tok", tmp: Path | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin"}
    if url is not None:
        env["NORTH_GATE_URL"] = url
    if token is not None:
        env["NORTH_GATE_TOKEN"] = token
    # An isolated interpreter in an empty directory: nothing of north is importable.
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(PAYLOAD), capture_output=True, text=True, env=env, cwd=tmp
    )


def test_an_allow_and_a_deny_are_printed_for_claude_code_to_read(serve, tmp_path) -> None:
    allowed = _run(serve(body=ALLOW).url, tmp=tmp_path)
    denied = _run(serve(body=DENY).url, tmp=tmp_path)

    assert (allowed.returncode, json.loads(allowed.stdout)) == (0, ALLOW)
    assert (denied.returncode, json.loads(denied.stdout)) == (0, DENY)


def test_a_pass_prints_nothing_so_the_vendor_decides(serve, tmp_path) -> None:
    result = _run(serve(body={"pass": True}).url, tmp=tmp_path)

    assert (result.returncode, result.stdout) == (0, "")


def test_it_sends_the_run_token_and_the_payload_untouched(serve, tmp_path) -> None:
    server = serve()

    _run(server.url, token="run-token", tmp=tmp_path)

    assert server.seen == [("run-token", PAYLOAD)]


def test_a_slow_human_is_waited_for(serve, tmp_path) -> None:
    result = _run(serve(delay=1.2).url, tmp=tmp_path)

    assert (result.returncode, json.loads(result.stdout)) == (0, ALLOW)


@pytest.mark.parametrize(
    "server_args",
    [
        {"status": 403},
        {"status": 500},
        {"body": b"not json"},
        {"body": {"nonsense": True}},
        {"body": {"hookSpecificOutput": {"permissionDecision": "ask"}}},
        {"body": {"pass": False}},
    ],
)
def test_every_kind_of_bad_answer_blocks(serve, tmp_path, server_args) -> None:
    result = _run(serve(**server_args).url, tmp=tmp_path)

    assert (result.returncode, result.stdout) == (2, "")
    assert "north gate" in result.stderr


def test_a_server_that_is_down_blocks(tmp_path) -> None:
    assert _run("http://127.0.0.1:9/gate", tmp=tmp_path).returncode == 2


@pytest.mark.parametrize(("url", "token"), [(None, "tok"), ("http://127.0.0.1:9/gate", None)])
def test_missing_configuration_blocks(url, token, tmp_path) -> None:
    assert _run(url, token, tmp=tmp_path).returncode == 2


def test_it_imports_nothing_from_north() -> None:
    source = HOOK.read_text()

    assert "coding_agents" not in source.replace("coding_agents/", "").replace("coding_agents.hook", "")
    assert "import north" not in source and "from approval" not in source
