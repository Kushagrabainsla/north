"""server_url: the address a coding hook or the callback server uses to reach north."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from config import server_url as module
from config.server_url import server_url
from config.settings import settings


@pytest.fixture
def unset_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """As on a server started without NORTH_NORTH_ORCHESTRATOR_URL."""
    monkeypatch.setattr(settings, "__pydantic_fields_set__", settings.model_fields_set - {"north_orchestrator_url"})
    monkeypatch.setattr(settings, "callback_port", 1)


@pytest.fixture
def listening() -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def test_a_server_on_another_port_is_reached_on_that_port(unset_url, listening: int) -> None:
    assert server_url() == f"http://127.0.0.1:{listening}"


def test_the_callback_servers_port_is_not_taken_for_norths(
    unset_url, listening: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "callback_port", listening)

    assert server_url() == settings.north_orchestrator_url


def test_the_setting_wins_when_it_was_set(listening: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "north_orchestrator_url", "http://north.internal:7000")

    assert server_url() == "http://north.internal:7000"


def test_a_socket_on_every_address_is_reached_on_loopback() -> None:
    assert module._reachable_host("0.0.0.0") == "127.0.0.1"
    assert module._reachable_host("::") == "127.0.0.1"
    assert module._reachable_host("::1") == "[::1]"
