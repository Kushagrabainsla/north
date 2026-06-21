"""Cryptographic key and FastAPI request authentication helpers.

See docs/CODING_STYLE.md Sections 5.2, 12.3.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from config.settings import read_secret_file, settings


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
    return secrets.compare_digest(secret_to_verify, stored_secret)


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
