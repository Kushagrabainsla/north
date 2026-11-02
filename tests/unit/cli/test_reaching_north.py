"""The CLI reaches the north running from its home, and sends the secret to nothing else.

In the issue #76 test (findings P2, P3, P7) a north started on port 8300 could
not be reached by any CLI command, `north status` printed "server down" and
exited 0, its provider list was a fixed one read from the CLI's own settings,
and when another local user listened on 8000 every command sent them the master
secret and showed their empty answers as fact.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import typer
from typer.testing import CliRunner

import cli._client as client
from cli import main
from config.run_file import read_run_file, remove_run_file, write_run_file
from config.security import load_secret, server_proof
from config.settings import settings
from orchestrator.api.health import prove_identity
from orchestrator.app import _record_where_this_server_listens


class _Server:
    """A local HTTP listener that records every header it was sent."""

    def __init__(self, *, genuine: bool) -> None:
        seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                seen.append(dict(self.headers))
                url = urlparse(self.path)
                if url.path == "/health/proof":
                    nonce = parse_qs(url.query)["nonce"][0]
                    body = {"proof": server_proof(nonce, load_secret()) if genuine else "0" * 64}
                else:
                    body = []
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.seen = seen
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def secrets_received(self) -> list[str]:
        return [headers["X-North-Secret"] for headers in self.seen if "X-North-Secret" in headers]

    def close(self) -> None:
        self._httpd.shutdown()


@pytest.fixture(autouse=True)
def _fresh_client(monkeypatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_genuine", set())


@pytest.fixture
def north() -> Iterator[_Server]:
    server = _Server(genuine=True)
    yield server
    server.close()


@pytest.fixture
def impostor() -> Iterator[_Server]:
    server = _Server(genuine=False)
    yield server
    server.close()


def test_the_cli_finds_north_on_the_port_it_runs_on(north: _Server) -> None:
    write_run_file(north.url)

    assert client.base_url() == north.url
    assert client.running_port() == urlparse(north.url).port
    client._api("GET", "/orchestrator/tasks")
    assert north.secrets_received() == [load_secret()]


def test_the_secret_is_never_sent_to_a_listener_that_is_not_this_north(impostor: _Server, monkeypatch) -> None:
    # Nothing in the run file: the CLI falls back to the configured address, where something else listens.
    monkeypatch.setattr(settings, "north_orchestrator_url", impostor.url)

    with pytest.raises(typer.Exit):
        client._api("GET", "/orchestrator/tasks")

    assert impostor.seen, "the impostor was asked to prove itself"
    assert impostor.secrets_received() == []


def test_without_a_running_north_the_default_port_is_used() -> None:
    assert read_run_file() is None
    assert client.running_port() == 8000


def test_a_run_file_is_private_and_names_only_a_live_process() -> None:
    write_run_file("http://127.0.0.1:8300")
    path = settings.north_home / "run" / "server.json"

    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    write_run_file("http://127.0.0.1:8300", pid=2**22 + 12345)  # no such process
    assert read_run_file() is None


def test_a_stopping_server_removes_only_its_own_run_file() -> None:
    write_run_file("http://127.0.0.1:8400", pid=os.getppid())  # a newer server, another process
    remove_run_file()
    assert read_run_file() is not None

    write_run_file("http://127.0.0.1:8400")
    remove_run_file()
    assert read_run_file() is None


async def test_the_server_records_where_it_listens_once_its_socket_is_bound(monkeypatch) -> None:
    answers = iter([None, None, "http://127.0.0.1:8300"])
    monkeypatch.setattr("orchestrator.app.listening_url", lambda: next(answers))

    await _record_where_this_server_listens(attempts=5, every=0)

    assert read_run_file() is not None and read_run_file().url == "http://127.0.0.1:8300"


async def test_the_server_proves_it_holds_the_secret() -> None:
    nonce = "ab" * 16
    assert (await prove_identity(nonce))["proof"] == server_proof(nonce, load_secret())


def test_status_lists_the_providers_the_server_has(monkeypatch) -> None:
    answers = {
        "/health": {"status": "ok", "checks": {}},
        "/orchestrator/settings": {"power": "cruise", "autonomy": "ask"},
        "/orchestrator/inference/models": {},
        "/web/api/system": {
            "providers": [{"id": "anthropic", "configured": True}, {"id": "gemini", "configured": False}]
        },
    }

    class _Answer:
        def __init__(self, body: object) -> None:
            self._body = body

        def json(self) -> object:
            return self._body

    monkeypatch.setattr(main, "_api", lambda method, path, **kw: _Answer(answers.get(path, {})))
    output = CliRunner().invoke(main.app, ["status"]).output

    providers = next(line for line in output.splitlines() if "providers" in line)
    assert "anthropic" in providers
    assert "gemini" not in providers and "openrouter" not in providers
