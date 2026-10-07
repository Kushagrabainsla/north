"""The webhook route: how Telegram and outside services submit tasks.

It had no test, and from the per-app wiring refactor on it answered every submission with a 500 because it
never bound the app's services to the request. Telegram submits through it, so Telegram tasks failed for a
month with nothing said to the user.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from config.security import load_secret
from ledger.models import LedgerSource
from orchestrator.api import webhook_router
from orchestrator.api_context import ApiServices, attach


class Orchestrator:
    def __init__(self) -> None:
        self.submitted = []

    async def submit_task(self, request):
        self.submitted.append(request)
        return type("Result", (), {"task_id": "t-1", "status": "queued"})()


@pytest.fixture
def orchestrator() -> Orchestrator:
    return Orchestrator()


@pytest.fixture
def client(orchestrator):
    app = FastAPI()
    attach(app, ApiServices(orchestrator=orchestrator))
    app.include_router(webhook_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://daemon")


async def test_a_submission_with_the_secret_becomes_a_task(client, orchestrator) -> None:
    response = await client.post(
        "/orchestrator/webhooks/telegram", json={"prompt": "hello"}, headers={"X-Webhook-Secret": load_secret()}
    )

    assert response.status_code == 202, response.text
    assert response.json() == {"task_id": "t-1", "status": "queued", "source": "telegram"}
    [request] = orchestrator.submitted
    assert request.prompt == "[webhook:telegram] hello" and request.source is LedgerSource.WEBHOOK


async def test_a_submission_without_the_secret_is_refused_and_starts_nothing(client, orchestrator) -> None:
    response = await client.post("/orchestrator/webhooks/telegram", json={"prompt": "hello"})

    assert response.status_code == 401
    assert orchestrator.submitted == []
