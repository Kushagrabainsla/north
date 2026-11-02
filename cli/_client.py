"""HTTP client for the north CLI.

Commands talk exclusively to the Orchestrator API of the north running from this
CLI's home: found through its run file, whatever its port, and trusted with the
secret only once it has proved it holds it.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

import httpx
import typer

from cli.constants import _TIMEOUT
from config.run_file import read_run_file
from config.security import load_secret, server_proof
from config.settings import settings

# Servers that answered this process's proof request correctly.
_genuine: set[str] = set()


class NotThisNorth(Exception):
    """The server at a URL did not prove it holds this home's secret."""


def base_url() -> str:
    """The north this CLI talks to: the one running from this home, else the configured address.

    The configured address is port 8000 unless set; it is where `north start` puts a server by default.
    """
    if (running := read_run_file()) is not None:
        return running.url
    return settings.north_orchestrator_url.rstrip("/")


def running_port(default: int = 8000) -> int:
    """The port this home's north listens on, else *default*: what a port option means when it is not given."""
    if (running := read_run_file()) is not None and (port := urlparse(running.url).port) is not None:
        return port
    return default


def _check_genuine(url: str) -> None:
    """Raise NotThisNorth unless the server at *url* proves it holds this home's secret.

    Asked before the secret is ever sent: anything else listening there, another
    user's server or a dev server on 8000, cannot answer and is told nothing.
    """
    if url in _genuine:
        return
    nonce = secrets.token_hex(16)
    try:
        response = httpx.get(f"{url}/health/proof", params={"nonce": nonce}, timeout=_TIMEOUT)
        proof = response.json().get("proof", "") if response.status_code == 200 else ""
    except ValueError:
        proof = ""
    if not isinstance(proof, str) or not secrets.compare_digest(proof, server_proof(nonce, load_secret())):
        raise NotThisNorth(url)
    _genuine.add(url)


def _headers(url: str | None = None) -> dict[str, str]:
    """The secret header, for a server at *url* (this home's north by default) that proved it is north."""
    _check_genuine((url or base_url()).rstrip("/"))
    return {"X-North-Secret": load_secret()}


# One client per CLI process - reused across calls so a command that makes several
# requests (submit + poll + ledger fetch) keeps the connection alive instead of
# paying TCP/TLS setup on every call.
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=base_url(), timeout=_TIMEOUT)
    return _client


def _api(method: str, path: str, **kwargs: object) -> httpx.Response:
    """Execute a synchronous HTTP call to the Orchestrator API."""
    client = _get_client()
    try:
        response = client.request(method, path, headers=_headers(str(client.base_url)), **kwargs)  # type: ignore[arg-type]
        response.raise_for_status()
        return response
    except httpx.ConnectError:
        typer.secho(
            f"ERROR: Cannot reach north at {client.base_url}. Is it running? Start it with: north start",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except NotThisNorth:
        typer.secho(
            f"ERROR: The server at {client.base_url} is not your north: it could not prove it holds "
            "this home's secret, so the secret was not sent. Start yours with: north start",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPStatusError as exc:
        typer.secho(
            f"ERROR: Server returned {exc.response.status_code}: {exc.response.text}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
