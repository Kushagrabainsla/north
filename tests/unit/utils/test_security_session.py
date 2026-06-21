"""Tests for request authentication (header-based shared secret)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from config.settings import settings
from utils.security import verify_request_secret

_TEST_SECRET = "unit-test-master-secret"


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    monkeypatch.setattr(settings, "north_secret", _TEST_SECRET)


async def test_request_auth_accepts_master_secret_header() -> None:
    await verify_request_secret(x_north_secret=_TEST_SECRET)


async def test_request_auth_rejects_wrong_secret() -> None:
    with pytest.raises(HTTPException):
        await verify_request_secret(x_north_secret="wrong-secret")


async def test_request_auth_rejects_missing_credentials() -> None:
    with pytest.raises(HTTPException):
        await verify_request_secret(x_north_secret=None)
