"""The one door out for sandboxed commands (#37).

A command running under the OS sandbox cannot open a network connection of its
own; the kernel allows it to reach only this proxy, on loopback. The proxy
checks the destination against an allowlist of domains, resolves the name
**once**, refuses any non-public address, and connects to that same address -
so a name that answers differently the second time (DNS rebinding) cannot
steer the connection somewhere else. Only ports 80 and 443 are reachable.

Programs that honour `HTTP_PROXY`/`HTTPS_PROXY` (pip, npm, git, curl, cargo)
work unchanged; one that ignores them cannot connect at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from utils.net import is_public_ip

logger = logging.getLogger(__name__)

# Package registries and code hosts that development commands need.
DEFAULT_ALLOWED_DOMAINS = (
    "pypi.org",
    "pythonhosted.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "github.com",
    "githubusercontent.com",
    "crates.io",
    "proxy.golang.org",
    "sum.golang.org",
)

_ALLOWED_PORTS = frozenset({80, 443})
_HEAD_LIMIT = 65_536
_HEAD_TIMEOUT = 15.0
_CHUNK = 65_536


class EgressDenied(Exception):
    """The destination is not allowed; the message says why, for the command's stderr."""


class EgressProxy:
    """A loopback HTTP/CONNECT proxy that only lets allowed public hosts through."""

    def __init__(self, allowed_domains: Iterable[str] = DEFAULT_ALLOWED_DOMAINS) -> None:
        self._allowed = tuple(d.strip().lower().strip(".") for d in allowed_domains if d.strip())
        self._server: asyncio.Server | None = None
        self.port: int | None = None

    async def start(self) -> int:
        """Listen on a free loopback port and return it. Safe to call again."""
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
        assert self.port is not None
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self.port = None

    def allows(self, host: str) -> bool:
        host = host.lower().strip(".")
        return any(host == domain or host.endswith("." + domain) for domain in self._allowed)

    async def _connect(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Check *host*, resolve it once, and connect to that address."""
        if port not in _ALLOWED_PORTS:
            raise EgressDenied(f"port {port} is not allowed (only 80 and 443)")
        if not self.allows(host):
            raise EgressDenied(f"{host} is not on north's allowed list")
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        if not addresses or not all(is_public_ip(ip) for ip in addresses):
            raise EgressDenied(f"{host} resolves to a non-public address")
        return await asyncio.open_connection(str(addresses[0]), port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_HEAD_TIMEOUT)
            if len(head) > _HEAD_LIMIT:
                raise EgressDenied("request header too large")
            lines = head.split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                up_reader, upstream = await self._connect(host.strip("[]"), int(port_text))
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise EgressDenied("only absolute http:// URLs and CONNECT are proxied")
                up_reader, upstream = await self._connect(parts.hostname, parts.port or 80)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                kept = [
                    line
                    for line in lines[1:-2]
                    if line and line.split(b":", 1)[0].strip().lower() not in (b"proxy-connection", b"connection")
                ]
                rebuilt = [f"{method} {path} {version}".encode("latin-1"), *kept, b"Connection: close", b"", b""]
                upstream.write(b"\r\n".join(rebuilt))
                await upstream.drain()
            await asyncio.gather(_pipe(reader, upstream), _pipe(up_reader, writer))
        except EgressDenied as denied:
            logger.info("Egress refused: %s", denied)
            body = f"north blocked this connection: {denied}\n".encode()
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n")
            writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            with contextlib.suppress(ConnectionError):
                await writer.drain()
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(_CHUNK):
            sink.write(chunk)
            await sink.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            sink.write_eof()
