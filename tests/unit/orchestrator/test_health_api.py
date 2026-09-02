from __future__ import annotations

from orchestrator import api_router


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


async def test_health_check_reports_real_ready_components(monkeypatch) -> None:
    monkeypatch.setattr(api_router, "_orchestrator", _Orchestrator())
    monkeypatch.setattr(api_router, "_ledger", _Ledger())
    monkeypatch.setattr(api_router, "_context_store", _Memory())
    monkeypatch.setattr(api_router, "_job_processor", _Jobs())
    monkeypatch.setattr(api_router, "_stream_manager", object())
    monkeypatch.setattr(api_router, "_inference_router", _Inference())

    result = await api_router.health_check()

    assert result["status"] == "ok"
    assert set(result["checks"]) == {
        "api", "orchestrator", "ledger", "memory", "scheduler", "event_stream", "inference"
    }
    assert result["checks"]["inference"]["models"] == 2


async def test_health_check_is_degraded_when_runtime_is_not_configured(monkeypatch) -> None:
    for name in (
        "_orchestrator", "_ledger", "_context_store", "_job_processor", "_stream_manager", "_inference_router"
    ):
        monkeypatch.setattr(api_router, name, None)

    result = await api_router.health_check()

    assert result["status"] == "degraded"
    assert result["checks"]["api"]["status"] == "ok"
    assert result["checks"]["ledger"]["status"] == "failed"
