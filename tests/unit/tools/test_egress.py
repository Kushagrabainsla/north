"""The egress proxy (#37): an allowlist, one resolution pinned, public addresses only."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import approving_store, bind_approvals
from tools.models import ToolInput
from tools.specialized import _egress, _os_sandbox
from tools.specialized._egress import EgressProxy
from tools.specialized.bash import BashTool


async def _ask(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    return reply


@pytest.fixture
async def proxy():
    p = EgressProxy(["example.test"])
    await p.start()
    yield p
    await p.stop()


def test_a_domain_and_its_subdomains_are_allowed_and_nothing_else() -> None:
    p = EgressProxy(["pypi.org"])

    assert p.allows("pypi.org") and p.allows("files.pypi.org") and p.allows("PyPI.org.")
    assert not p.allows("evilpypi.org") and not p.allows("pypi.org.evil.com") and not p.allows("127.0.0.1")


@pytest.mark.asyncio
async def test_a_host_off_the_list_gets_403(proxy) -> None:
    reply = await _ask(proxy.port, b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n")

    assert reply.startswith(b"HTTP/1.1 403")
    assert b"not on north's allowed list" in reply


@pytest.mark.asyncio
async def test_only_ports_80_and_443_are_reachable(proxy) -> None:
    reply = await _ask(proxy.port, b"CONNECT example.test:22 HTTP/1.1\r\n\r\n")

    assert reply.startswith(b"HTTP/1.1 403") and b"port 22" in reply


@pytest.mark.asyncio
async def test_an_allowed_name_that_resolves_to_a_private_address_is_refused(proxy, monkeypatch) -> None:
    async def private(self, host, port, **kw):
        return [(2, 1, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", private)

    reply = await _ask(proxy.port, b"CONNECT example.test:443 HTTP/1.1\r\n\r\n")

    assert reply.startswith(b"HTTP/1.1 403") and b"non-public" in reply


@pytest.mark.asyncio
async def test_the_connection_goes_to_the_address_that_was_checked(proxy, monkeypatch) -> None:
    """Resolved once, connected to that address: a second answer from DNS cannot redirect it."""
    answers = iter(["93.184.216.34", "127.0.0.1"])

    async def rebinding(self, host, port, **kw):
        return [(2, 1, 6, "", (next(answers), port))]

    reached: list[str] = []

    async def fake_open(host, port, **kw):
        reached.append(host)
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", rebinding)
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    monkeypatch.setattr(_egress.asyncio, "open_connection", fake_open)

    with pytest.raises(ConnectionRefusedError):
        await proxy._connect("example.test", 443)

    assert reached == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_an_allowed_host_is_tunnelled_and_proxied(monkeypatch) -> None:
    async def upstream(reader, writer):
        data = await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi" if data.startswith(b"GET /x") else b"pong")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    target = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(_egress, "is_public_ip", lambda ip: True)
    monkeypatch.setattr(_egress, "_ALLOWED_PORTS", frozenset({target}))
    p = EgressProxy(["localhost"])
    port = await p.start()
    try:
        plain = await _ask(port, f"GET http://localhost:{target}/x HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT localhost:{target} HTTP/1.1\r\n\r\n".encode())
        established = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ping")
        tunnelled = await asyncio.wait_for(reader.read(100), timeout=5)
        writer.close()
    finally:
        await p.stop()
        server.close()

    assert plain.endswith(b"hi")
    assert established.startswith(b"HTTP/1.1 200")
    assert tunnelled == b"pong"


@pytest.mark.skipif(_os_sandbox.current() is None, reason="needs macOS Seatbelt")
class TestApprovedCommandsReachOnlyTheProxy:
    @staticmethod
    async def _run(tmp_path, command: str):
        tool = bind_approvals(BashTool(os_sandbox=True, allowed_domains=("example.test",)), store=approving_store())
        try:
            return await tool.execute(ToolInput(params={"command": command, "workspace": str(tmp_path)}))
        finally:
            await tool.aclose()

    @pytest.mark.asyncio
    async def test_a_host_off_the_list_is_blocked_through_the_proxy(self, tmp_path) -> None:
        https = await self._run(tmp_path, "touch f; curl -sS -m 5 https://evil.example/")
        http = await self._run(tmp_path, "touch f; curl -sS -m 5 http://evil.example/")

        assert "response 403" in (https.error or "")
        assert "not on north's allowed list" in str(http.data["stdout"])

    @pytest.mark.asyncio
    async def test_a_program_that_ignores_the_proxy_cannot_connect_at_all(self, tmp_path) -> None:
        """Refused at once by the kernel - a timeout would not tell a block from a dead host."""
        out = await self._run(tmp_path, "touch f; curl -sS -m 5 --noproxy '*' https://1.1.1.1/")

        assert not out.success
        assert "couldn't connect" in (out.error or "").lower() or "not permitted" in (out.error or "").lower()

    @pytest.mark.asyncio
    async def test_a_local_server_stays_reachable(self, tmp_path) -> None:
        """A project's own tests start servers on loopback; the sandbox must not break them."""

        async def reply(reader, writer):
            await reader.read(1024)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(reply, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            out = await self._run(tmp_path, f"touch f; curl -sS -m 5 http://127.0.0.1:{port}/")
        finally:
            server.close()

        assert out.success, out.error
        assert out.data["stdout"] == "ok"
