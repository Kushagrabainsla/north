"""Cryptographic key and FastAPI request authentication helpers.

See docs/CODING_STYLE.md Sections 5.2, 12.3.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from config.settings import read_secret_file, settings

WEB_SESSION_COOKIE = "north_web_session"
_WEB_SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class WebSession:
    token: str
    csrf: str
    expires_at: float


_web_sessions: dict[str, WebSession] = {}


def issue_web_session() -> WebSession:
    """Create an invisible, short-lived session for the loopback web UI."""
    now = time.time()
    expired = [token for token, session in _web_sessions.items() if session.expires_at <= now]
    for token in expired:
        _web_sessions.pop(token, None)
    session = WebSession(
        token=secrets.token_urlsafe(32),
        csrf=secrets.token_urlsafe(24),
        expires_at=now + _WEB_SESSION_TTL_SECONDS,
    )
    _web_sessions[session.token] = session
    return session


def _loopback_request(request: Request) -> bool:
    return (request.url.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def validate_web_session(request: Request, csrf: str | None = None) -> bool:
    """Validate an automatically issued loopback browser session."""
    if not _loopback_request(request):
        return False
    token = request.cookies.get(WEB_SESSION_COOKIE, "")
    session = _web_sessions.get(token)
    if session is None or session.expires_at <= time.time():
        _web_sessions.pop(token, None)
        return False
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        if not csrf or not secrets.compare_digest(csrf, session.csrf):
            return False
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return False
    return True


def generate_secret() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_hex(32)


def load_secret() -> str:
    """Load the shared secret from north_home, creating it if it does not exist."""
    secret_file = settings.north_home / "secret.key"
    if secret_file.exists():
        return read_secret_file(secret_file)

    settings.north_home.mkdir(parents=True, exist_ok=True)
    secret = generate_secret()
    try:
        # Exclusive create - raises FileExistsError if another process won the race.
        with secret_file.open("x") as f:
            f.write(secret)
        secret_file.chmod(0o600)
    except FileExistsError:
        return read_secret_file(secret_file)
    return secret


def verify_secret(secret_to_verify: str) -> bool:
    """Verify if the provided secret matches the canonical shared secret."""
    stored_secret = settings.secret
    if not stored_secret:
        stored_secret = load_secret()
    if secrets.compare_digest(secret_to_verify, stored_secret):
        return True

    # The server caches file-backed secrets for performance. If the key file was
    # rotated while this process stayed alive, retry once from disk and refresh
    # the cache so running servers accept the new key without a restart.
    if settings.north_secret:
        return False
    secret_file = settings.north_home / "secret.key"
    if not secret_file.exists():
        return False
    refreshed_secret = read_secret_file(secret_file)
    settings._secret_cache = refreshed_secret
    return secrets.compare_digest(secret_to_verify, refreshed_secret)


async def verify_request_secret(
    x_north_secret: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: require the shared-secret header.

    Used by the CLI and external clients; the header carries the master secret.

    Raises:
        HTTPException: 403 if the header is missing or does not match.
    """
    if x_north_secret and verify_secret(x_north_secret):
        return
    raise HTTPException(status_code=403, detail="Invalid secret.")


async def verify_api_access(
    request: Request,
    x_north_secret: str | None = Header(default=None),
    x_north_csrf: str | None = Header(default=None),
) -> None:
    """Accept either the CLI's master-secret header or a local web session."""
    if x_north_secret and verify_secret(x_north_secret):
        return
    if validate_web_session(request, x_north_csrf):
        return
    raise HTTPException(status_code=403, detail="Invalid credentials.")
