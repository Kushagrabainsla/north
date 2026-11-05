"""Regression tests for hot secret-key rotation while the server is running."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from config.security import verify_api_access, verify_secret
from config.settings import settings


def _request() -> Request:
    """A request with no browser session, so only the header can let it in."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def test_verify_secret_reloads_rotated_file_backed_key(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "secret.key"
    key_path.write_text("old-secret", encoding="utf-8")
    key_path.chmod(0o600)

    monkeypatch.setattr(settings, "north_home", tmp_path)
    monkeypatch.setattr(settings, "north_secret", "")
    monkeypatch.setattr(settings, "_secret_cache", "")

    assert verify_secret("old-secret") is True
    key_path.write_text("new-secret", encoding="utf-8")

    assert verify_secret("new-secret") is True
    assert verify_secret("old-secret") is False


async def test_request_auth_accepts_rotated_secret_without_restart(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "secret.key"
    key_path.write_text("old-secret", encoding="utf-8")
    key_path.chmod(0o600)

    monkeypatch.setattr(settings, "north_home", tmp_path)
    monkeypatch.setattr(settings, "north_secret", "")
    monkeypatch.setattr(settings, "_secret_cache", "")

    await verify_api_access(_request(), x_north_secret="old-secret", x_north_csrf=None)
    key_path.write_text("new-secret", encoding="utf-8")

    await verify_api_access(_request(), x_north_secret="new-secret", x_north_csrf=None)
    with pytest.raises(HTTPException):
        await verify_api_access(_request(), x_north_secret="old-secret", x_north_csrf=None)
