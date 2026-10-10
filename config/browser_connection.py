"""Local CDP connection validation, owned by browser configuration."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def cdp_http_base(connect: str) -> str:
    raw = connect.strip()
    if not raw:
        raise ValueError("Existing-browser access requires a CDP connection endpoint.")
    if raw.isdigit():
        raw = f"http://127.0.0.1:{raw}"
    elif "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise ValueError("CDP endpoint must be a port or an http(s)/ws(s) URL.")
    if parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("CDP connections are restricted to this machine (localhost only).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CDP endpoints cannot contain credentials, query strings, or fragments.")
    scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{host}{port}"


def active_cdp_endpoint(directory: Path) -> str:
    """Read only the selected browser's published endpoint, never scan ports."""
    try:
        lines = (directory / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        websocket = lines[1]
        if not 0 < port < 65536 or not websocket.startswith("/devtools/browser/"):
            raise ValueError("Invalid Chrome endpoint file.")
        endpoint = f"ws://127.0.0.1:{port}{websocket}"
        cdp_http_base(endpoint)
        return endpoint
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError(
            "This browser has not published a usable connection. Open Chrome's "
            "chrome://inspect/#remote-debugging, enable it, and allow the connection. "
            "North will not restart Chrome or copy your login data."
        ) from exc
