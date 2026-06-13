"""HTTP client for the north CLI.

Commands talk exclusively to the Orchestrator API.
"""

from __future__ import annotations

import httpx
import typer

from cli.constants import _BASE_URL, _TIMEOUT
from utils.security import load_secret


def _headers() -> dict[str, str]:
    return {"X-North-Secret": load_secret()}


def _api(method: str, path: str, **kwargs: object) -> httpx.Response:
    """Execute a synchronous HTTP call to the Orchestrator API."""
    url = f"{_BASE_URL}{path}"
    try:
        response = httpx.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)  # type: ignore[arg-type]
        response.raise_for_status()
        return response
    except httpx.ConnectError:
        typer.secho(
            "ERROR: Cannot reach the north server. Is it running?\n  uvicorn orchestrator.app:app --port 8000",
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
