"""Cryptographic key and FastAPI request authentication helpers.

See docs/CODING_STYLE.md Sections 5.2, 12.3.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Cookie, Header, HTTPException

from config.settings import read_secret_file, settings

# Web UI session cookie name. Holds a signed, expiring session token derived
# from the master secret — never the master secret itself.
SESSION_COOKIE = "north_session"
_SESSION_TTL_SECONDS = 7 * 24 * 3600


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
        # Exclusive create — raises FileExistsError if another process won the race.
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
    return secrets.compare_digest(secret_to_verify, stored_secret)


def _sign_session(expires_at: int) -> str:
    key = (settings.secret or load_secret()).encode("utf-8")
    return hmac.new(key, f"north-session:{expires_at}".encode(), hashlib.sha256).hexdigest()


def issue_session_token() -> str:
    """Return a signed, expiring Web UI session token.

    The token is an HMAC over an expiry timestamp keyed by the master secret —
    it authenticates a browser session without ever placing the master secret
    in a cookie, URL, or log line.
    """
    expires_at = int(time.time()) + _SESSION_TTL_SECONDS
    return f"{expires_at}.{_sign_session(expires_at)}"


def verify_session_token(token: str) -> bool:
    """True when *token* is a validly signed, unexpired session token."""
    expiry_str, sep, signature = token.partition(".")
    if not sep or not expiry_str.isdigit():
        return False
    expires_at = int(expiry_str)
    if expires_at < time.time():
        return False
    return hmac.compare_digest(signature, _sign_session(expires_at))


async def verify_request_secret(
    x_north_secret: str | None = Header(default=None),
    north_session: str | None = Cookie(default=None),
) -> None:
    """FastAPI dependency: accept the shared secret header or a session cookie.

    The header path is used by the CLI and external clients and carries the
    master secret. The cookie path is used by the Web UI and carries a signed
    session token (set by POST /ui/auth) — never the master secret.

    Raises:
        HTTPException: 403 if neither credential is present or valid.
    """
    if x_north_secret and verify_secret(x_north_secret):
        return
    if north_session and verify_session_token(north_session):
        return
    raise HTTPException(status_code=403, detail="Invalid secret.")
