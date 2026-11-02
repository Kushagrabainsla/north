"""Only the owner can sign a browser in to north; being on the same machine is not enough.

In the issue #76 test (finding P1) the OS user `nobody`, who cannot read
secret.key, got a full dashboard session with one `POST /web/session`, switched
north to yolo, read the owner's conversations and artifacts, and started a task
that ran on the owner's API key. The session was issued to any process that
could reach loopback.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config.security as security
from config.security import issue_pairing, keep_home_private, load_secret
from config.settings import settings
from orchestrator.api_context import ApiServices, attach
from web.api import router, session_router
from web.browser_keys import BROWSER_KEY_COOKIE


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    attach(app, ApiServices(north_home=settings.north_home))
    app.include_router(session_router)
    app.include_router(router)
    return app


def _browser(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def test_another_local_process_gets_nothing(app: FastAPI) -> None:
    nobody = _browser(app)

    refused = nobody.post("/web/session")

    assert refused.status_code == 401
    assert "north web" in refused.json()["detail"]
    assert nobody.get("/web/api/notices").status_code == 403


def test_the_owner_signs_one_browser_in_with_a_one_time_link(app: FastAPI) -> None:
    owner_cli = _browser(app)
    link = owner_cli.post("/web/api/pair", headers={"X-North-Secret": load_secret()}).json()
    token = link["path"].removeprefix("/app/?pair=")

    browser = _browser(app)
    session = browser.post("/web/session", json={"pair": token})
    assert session.status_code == 200
    assert browser.get("/web/api/notices").status_code == 200

    # The link works once: someone who sees it afterwards gets nothing.
    assert _browser(app).post("/web/session", json={"pair": token}).status_code == 401


def test_asking_for_a_link_needs_the_secret(app: FastAPI) -> None:
    assert _browser(app).post("/web/api/pair").status_code == 403
    assert _browser(app).post("/web/api/pair", headers={"X-North-Secret": "guess"}).status_code == 403


def test_an_old_link_does_not_sign_anyone_in(app: FastAPI, monkeypatch) -> None:
    token, _ = issue_pairing()
    later = time.time() + 11 * 60
    monkeypatch.setattr(security.time, "time", lambda: later)

    assert _browser(app).post("/web/session", json={"pair": token}).status_code == 401


def test_a_signed_in_browser_stays_signed_in_across_a_restart(app: FastAPI, monkeypatch) -> None:
    browser = _browser(app)
    browser.post("/web/session", json={"pair": issue_pairing()[0]})
    # A restart forgets every session; the browser renews its own with the cookie it keeps.
    monkeypatch.setattr(security, "_web_sessions", {})

    renewed = browser.post("/web/session")

    assert renewed.status_code == 200
    assert browser.get("/web/api/notices").status_code == 200


def test_only_a_hash_of_a_browser_key_is_stored(app: FastAPI) -> None:
    browser = _browser(app)
    browser.post("/web/session", json={"pair": issue_pairing()[0]})
    key = browser.cookies[BROWSER_KEY_COOKIE]

    with sqlite3.connect(settings.north_home / "web_access.db") as conn:
        stored = [row[0] for row in conn.execute("SELECT key_hash FROM browser_keys")]
    assert len(stored) == 1 and key not in stored[0]


def test_norths_home_is_made_private() -> None:
    settings.north_home.chmod(0o755)

    keep_home_private()

    assert settings.north_home.stat().st_mode & 0o777 == 0o700
