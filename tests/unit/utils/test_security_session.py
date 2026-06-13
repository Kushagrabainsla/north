"""Tests for Web UI session tokens and request auth (review finding R1#3)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from config.settings import settings
from utils import security
from utils.security import issue_session_token, verify_request_secret, verify_session_token

_TEST_SECRET = "unit-test-master-secret"


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    monkeypatch.setattr(settings, "north_secret", _TEST_SECRET)


def test_session_token_round_trip() -> None:
    token = issue_session_token()
    assert verify_session_token(token) is True


def test_session_token_never_contains_master_secret() -> None:
    assert _TEST_SECRET not in issue_session_token()


def test_tampered_token_rejected() -> None:
    token = issue_session_token()
    expiry, _, signature = token.partition(".")
    assert verify_session_token(f"{expiry}.{'0' * len(signature)}") is False


def test_expiry_cannot_be_forged() -> None:
    token = issue_session_token()
    expiry, _, signature = token.partition(".")
    assert verify_session_token(f"{int(expiry) + 9999}.{signature}") is False


def test_expired_token_rejected() -> None:
    past = 1_000_000  # 1970 — long expired
    assert verify_session_token(f"{past}.{security._sign_session(past)}") is False


def test_garbage_token_rejected() -> None:
    assert verify_session_token("") is False
    assert verify_session_token("not-a-token") is False
    assert verify_session_token("abc.def") is False


async def test_request_auth_accepts_master_secret_header() -> None:
    await verify_request_secret(x_north_secret=_TEST_SECRET, north_session=None)


async def test_request_auth_accepts_session_cookie() -> None:
    await verify_request_secret(x_north_secret=None, north_session=issue_session_token())


async def test_request_auth_rejects_master_secret_in_cookie() -> None:
    """The master secret is not a valid cookie value — only session tokens are."""
    with pytest.raises(HTTPException):
        await verify_request_secret(x_north_secret=None, north_session=_TEST_SECRET)


async def test_request_auth_rejects_missing_credentials() -> None:
    with pytest.raises(HTTPException):
        await verify_request_secret(x_north_secret=None, north_session=None)
