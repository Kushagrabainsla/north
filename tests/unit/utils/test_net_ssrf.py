"""Tests for the SSRF guard in utils.net (review finding R1#1)."""

from __future__ import annotations

import socket

import pytest

from utils import net
from utils.net import UnsafeUrlError, validate_public_url


def _fake_getaddrinfo(ip: str):
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918 private
        "172.16.1.1",  # RFC1918 private
        "192.168.1.10",  # RFC1918 private
        "169.254.169.254",  # link-local / cloud metadata
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 ULA (private)
    ],
)
def test_blocks_non_public_addresses(monkeypatch, blocked_ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(blocked_ip))
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://internal.example.com/admin")


def test_allows_public_address(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    validate_public_url("https://example.com/")  # must not raise


def test_rejects_non_http_schemes() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        validate_public_url("ftp://example.com/x")


def test_rejects_missing_hostname() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http:///nohost")


def test_rejects_unresolvable_host(monkeypatch) -> None:
    def boom(host, port, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://does-not-resolve.invalid/")


def test_fetch_validates_every_redirect_hop(monkeypatch) -> None:
    """A public host redirecting to a private one must be blocked at the hop."""
    validated: list[str] = []

    def fake_validate(url: str) -> None:
        validated.append(url)
        if "internal" in url:
            raise UnsafeUrlError("blocked")

    class FakeResponse:
        is_redirect = True
        headers = {"location": "http://internal.example/secret"}

        def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def build_request(self, method, url, **kwargs):
            return (method, url)

        def send(self, request, stream=False):
            return FakeResponse()

    monkeypatch.setattr(net, "validate_public_url", fake_validate)
    monkeypatch.setattr(net.httpx, "Client", FakeClient)

    with pytest.raises(UnsafeUrlError):
        net.fetch_url_text("https://public.example.com/start")
    assert any("internal" in url for url in validated)
