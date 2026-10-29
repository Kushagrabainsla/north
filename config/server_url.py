"""Where this north server can be reached, for callers it hands a URL to.

A coding agent's hooks and the Telegram callback server call back into north
over HTTP. The address came from the ``north_orchestrator_url`` setting, which
defaults to port 8000, so a server started on any other port, as with
``uvicorn orchestrator.app:app --port 9000``, sent every hook to whatever
listened on 8000, and each one failed with a 403. ``north start`` already sets
the variable; this covers the other ways of starting the server.

The port is looked up when a URL is needed, not at import or startup: uvicorn
binds its socket after the app's startup has run.
"""

from __future__ import annotations

import ipaddress
import logging
import os

import psutil

from config.settings import settings

logger = logging.getLogger(__name__)

_SETTING = "north_orchestrator_url"


def server_url() -> str:
    """This server's base URL: the setting when it was set, else the port the process listens on."""
    if _SETTING in settings.model_fields_set:
        return settings.north_orchestrator_url
    address = _listening_address()
    if address is None:
        return settings.north_orchestrator_url
    host, port = address
    return f"http://{host}:{port}"


def _listening_address() -> tuple[str, int] | None:
    """The one TCP address this process listens on, besides the callback server; None when not exactly one."""
    try:
        connections = psutil.Process(os.getpid()).net_connections(kind="tcp")
    except (psutil.Error, OSError):
        logger.debug("could not list this process's sockets", exc_info=True)
        return None
    addresses = {
        (_reachable_host(c.laddr.ip), c.laddr.port)
        for c in connections
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port != settings.callback_port
    }
    if len(addresses) != 1:
        return None
    return addresses.pop()


def _reachable_host(ip: str) -> str:
    """A host a local caller can connect to for a socket bound to ``ip``."""
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if parsed.is_unspecified:  # 0.0.0.0 or :: listens on loopback too
        return "127.0.0.1"
    return f"[{ip}]" if parsed.version == 6 else ip
