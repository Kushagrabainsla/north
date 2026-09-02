"""The /health probe reports the real state of each runtime component."""

from __future__ import annotations

import pytest

from orchestrator import api_router
from orchestrator.api_context import ApiServices, bind_services


class _Orchestrator:
    async def list_active_tasks(self):
        return []


class _Ledger:
    async def query(self, _filters):
        return []


class _Memory:
    async def read(self, _document):
        return ""


class _Jobs:
    async def list_jobs(self, *, limit: int):
        return []


class _Inference:
    def health_summary(self):
        return {"ready": True, "models": 2, "providers": 1}


@pytest.mark.asyncio
async def test_health_check_reports_real_ready_components() -> None:
    services = ApiServices(
        orchestrator=_Orchestrator(),
        ledger=_Ledger(),
        context_store=_Memory(),
        job_processor=_Jobs(),
        stream_manager=object(),
        inference_router=_Inference(),
    )

    with bind_services(services):
        result = await api_router.health_check()

    assert result["status"] == "ok"
    assert set(result["checks"]) == {
        "api", "orchestrator", "ledger", "memory", "scheduler", "event_stream", "inference"
    }
    assert result["checks"]["inference"]["models"] == 2


@pytest.mark.asyncio
async def test_health_check_is_degraded_when_runtime_is_not_configured() -> None:
    with bind_services(ApiServices()):
        result = await api_router.health_check()

    assert result["status"] == "degraded"
    assert result["checks"]["api"]["status"] == "ok"
    assert result["checks"]["ledger"]["status"] == "failed"


@pytest.mark.asyncio
async def test_two_apps_can_hold_different_wiring() -> None:
    """The point of moving off module globals: no shared mutable state."""
    from fastapi import FastAPI

    from orchestrator.api_context import attach, services_of

    ready, empty = FastAPI(), FastAPI()
    attach(ready, ApiServices(ledger=_Ledger()))
    attach(empty, ApiServices())

    assert services_of(ready).ledger is not None
    assert services_of(empty).ledger is None
