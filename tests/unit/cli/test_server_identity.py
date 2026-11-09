"""The CLI and the server agree on the secret; another north is not a stray process; IPv6 and Docker work."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import httpx
import pytest
import typer

from cli import _client
from cli import main as cli
from cli._client import NotThisNorth, OtherNorth, _check_genuine
from cli._server import _is_other_north, _port_in_use, url_host
from config.security import effective_secret, server_proof
from config.settings import settings


@pytest.fixture(autouse=True)
def _forget_proofs() -> None:
    _client._genuine.clear()


def _answers(monkeypatch: pytest.MonkeyPatch, answer) -> None:
    def fake_get(url: str, params: dict, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"proof": answer(params["nonce"])})

    monkeypatch.setattr(_client.httpx, "get", fake_get)


def test_a_secret_set_in_the_environment_is_the_one_the_cli_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "north_secret", "from-the-environment")

    assert effective_secret() == "from-the-environment"
    _answers(monkeypatch, lambda nonce: server_proof(nonce, "from-the-environment"))

    _check_genuine("http://x")


def test_a_key_file_that_changed_is_read_afresh(monkeypatch: pytest.MonkeyPatch) -> None:
    (settings.north_home / "secret.key").write_text("first", encoding="utf-8")
    assert effective_secret() == "first"

    (settings.north_home / "secret.key").write_text("second", encoding="utf-8")

    assert effective_secret() == "second"
    _answers(monkeypatch, lambda nonce: server_proof(nonce, "second"))
    _check_genuine("http://x")


def test_a_north_with_a_different_secret_is_told_apart_from_a_stranger(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, lambda nonce: server_proof(nonce, "someone-elses"))
    with pytest.raises(OtherNorth):
        _check_genuine("http://x")

    _client._genuine.clear()
    monkeypatch.setattr(_client.httpx, "get", lambda *a, **k: httpx.Response(404))
    with pytest.raises(NotThisNorth) as stranger:
        _check_genuine("http://x")
    assert not isinstance(stranger.value, OtherNorth)


def test_a_port_held_by_another_north_is_never_offered_for_killing(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, lambda nonce: server_proof(nonce, "someone-elses"))
    monkeypatch.setattr(cli, "_port_in_use", lambda host, port: True)
    confirm = MagicMock()
    monkeypatch.setattr(cli.typer, "confirm", confirm)
    options = MagicMock(host="127.0.0.1", port=8000)

    assert _is_other_north("127.0.0.1", 8000)
    with pytest.raises(typer.Exit):
        cli._free_the_port(options)

    confirm.assert_not_called()


def test_an_ipv6_host_is_bracketed_in_a_url() -> None:
    assert url_host("::1") == "[::1]"
    assert url_host("127.0.0.1") == "127.0.0.1"


def test_the_port_check_works_on_ipv6() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6 here")
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("::1", 0))
        except OSError:
            pytest.skip("no IPv6 loopback here")
        listener.listen()
        port = listener.getsockname()[1]

        assert _port_in_use("::1", port)


def test_a_free_port_is_free() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert not _port_in_use("127.0.0.1", port)


def test_docker_gets_the_secret_before_it_is_asked_to_prove_it(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(cli, "_print_start_header", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_wait_for_any_answer", lambda *a, **k: order.append("answered"))
    monkeypatch.setattr(cli, "_sync_docker_secret", lambda *a, **k: order.append("copied"))
    monkeypatch.setattr(cli, "_wait_for_server", lambda *a, **k: order.append("proved"))
    options = MagicMock(port=8000, workspace="/w", chat=False)

    cli._start_with_docker(options, MagicMock())

    assert order == ["answered", "copied", "proved"]
