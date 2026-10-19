"""SSRF-safe HTTP fetching shared by every component that retrieves a URL.

A model-supplied URL must never reach loopback, private, link-local
(including cloud metadata at 169.254.169.254), or otherwise non-public
addresses. ``fetch_url_text`` validates the destination's resolved IPs
before the request and re-validates after every redirect hop, and caps
both response size and total time.
"""

from __future__ import annotations

import ipaddress
import math
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 2_000_000  # 2 MB of body is plenty for readable text
_DEFAULT_TIMEOUT = 20.0

# Hosts that ask for a gap between requests. arXiv's API terms allow one request
# every three seconds and answer 429 "Rate exceeded" to faster callers; a
# briefing fetches six feeds and then checks its papers with one more request.
_MIN_INTERVAL_S: dict[str, float] = {"export.arxiv.org": 3.0}
_last_request_at: dict[str, float] = {}
_spacing = threading.Lock()


def _wait_for_turn(url: str) -> None:
    """Block until *url*'s host may be asked again. Hosts not listed never wait."""
    host = (urlparse(url).hostname or "").lower()
    gap = _MIN_INTERVAL_S.get(host)
    if gap is None:
        return
    with _spacing:
        wait = _last_request_at.get(host, -math.inf) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[host] = time.monotonic()


@dataclass
class FetchedText:
    """Decoded body of a successfully fetched URL."""

    url: str  # final URL after redirects
    text: str
    content_type: str


class UnsafeUrlError(ValueError):
    """The URL targets a private/internal network or is malformed."""


def _resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {host!r}: {exc}") from exc
    ips = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not ips:
        raise UnsafeUrlError(f"Host {host!r} resolved to no usable addresses.")
    return ips


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for a publicly routable address - not loopback, private, link-local, multicast or reserved."""
    return not (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def validate_public_url(url: str) -> None:
    """Raise UnsafeUrlError unless *url* is http(s) to a publicly routable host.

    Resolves the hostname and rejects loopback, private (RFC 1918/4193),
    link-local (incl. the 169.254.169.254 metadata endpoint), multicast,
    reserved, and unspecified addresses.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("URL must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no hostname.")
    for ip in _resolve_host_ips(host):
        if not is_public_ip(ip):
            raise UnsafeUrlError(f"URL host {host!r} resolves to non-public address {ip} - blocked.")


def fetch_url_text(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> FetchedText:
    """Fetch *url* and return its decoded body, enforcing the SSRF policy.

    Redirects are followed manually (up to _MAX_REDIRECTS) so every hop is
    re-validated against the private-network blocklist. The body read is
    capped at _MAX_RESPONSE_BYTES. Synchronous - call via asyncio.to_thread.

    Raises:
        UnsafeUrlError: destination (or a redirect hop) is not public.
        httpx.HTTPStatusError / httpx.RequestError: transport-level failures.
    """
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            validate_public_url(current)
            _wait_for_turn(current)
            request = client.build_request("GET", current, headers={"User-Agent": "north/1.0 (personal AI assistant)"})
            response = client.send(request, stream=True)
            try:
                if response.is_redirect:
                    next_url = response.headers.get("location", "")
                    current = str(httpx.URL(current).join(next_url))
                    continue
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        break
                encoding = response.encoding or "utf-8"
                return FetchedText(
                    url=current,
                    text=bytes(body[:_MAX_RESPONSE_BYTES]).decode(encoding, errors="replace"),
                    content_type=response.headers.get("content-type", ""),
                )
            finally:
                response.close()
    raise UnsafeUrlError(f"Too many redirects (> {_MAX_REDIRECTS}) fetching {url}")
