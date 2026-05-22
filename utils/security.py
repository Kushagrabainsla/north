"""Cryptographic key and FastAPI request authentication helpers.

See docs/CODING_STYLE.md Sections 5.2, 12.3.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Header, HTTPException

from config.settings import settings


def generate_secret() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_hex(32)


def load_secret() -> str:
    """Load the shared secret from north_home, creating it if it does not exist."""
    secret_file = settings.north_home / "secret.key"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    settings.north_home.mkdir(parents=True, exist_ok=True)
    secret = generate_secret()
    secret_file.write_text(secret, encoding="utf-8")
    return secret


def verify_secret(secret_to_verify: str) -> bool:
    """Verify if the provided secret matches the canonical shared secret."""
    stored_secret = settings.secret
    if not stored_secret:
        stored_secret = load_secret()
    return secrets.compare_digest(secret_to_verify, stored_secret)


async def verify_request_secret(x_north_secret: str = Header(...)) -> None:
    """FastAPI header dependency to verify request auth.

    Raises:
        HTTPException: 403 if secret verification fails.
    """
    if not verify_secret(x_north_secret):
        raise HTTPException(status_code=403, detail="Invalid secret.")
