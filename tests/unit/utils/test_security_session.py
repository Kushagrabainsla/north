"""Tests for request authentication (header-based shared secret)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from config.security import verify_api_access
from config.settings import settings

_TEST_SECRET = "unit-test-master-secret"


def _request() -> Request:
    """A request with no browser session, so only the header can let it in."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    monkeypatch.setattr(settings, "north_secret", _TEST_SECRET)


async def test_request_auth_accepts_master_secret_header() -> None:
    await verify_api_access(_request(), x_north_secret=_TEST_SECRET, x_north_csrf=None)


async def test_request_auth_rejects_wrong_secret() -> None:
    with pytest.raises(HTTPException):
        await verify_api_access(_request(), x_north_secret="wrong-secret", x_north_csrf=None)


async def test_request_auth_rejects_missing_credentials() -> None:
    with pytest.raises(HTTPException):
        await verify_api_access(_request(), x_north_secret=None, x_north_csrf=None)
