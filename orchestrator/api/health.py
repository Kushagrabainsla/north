"""Unauthenticated readiness probe."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request

from ledger.base import LedgerFilters
from memory.models import ContextDocument
from orchestrator.api.deps import health_router
from orchestrator.api_context import current_services, services_of


@health_router.get("/health", include_in_schema=True)
async def health_check(request: Request = None) -> dict:  # noqa: RUF013 - callable directly in tests
    """Readiness probe for the runtime components required to operate North.

    The route remains unauthenticated for Docker/load-balancer probes, so it is
    not covered by the router-level services binding and reads them from the app
    directly. It always returns HTTP 200 so the browser can tell a struggling API
    apart from an unreachable one: status=starting while a dependency is still
    warming up, degraded once something has actually failed.
    """
    services = services_of(request.app) if request is not None else current_services()
    checks: dict[str, dict[str, Any]] = {"api": {"status": "ok"}}

    async def run_check(name: str, operation) -> None:
        try:
            await asyncio.wait_for(operation, timeout=1.0)
            checks[name] = {"status": "ok"}
        except Exception as exc:
            checks[name] = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    async def probe(name: str, component: Any, operation) -> None:
        if component is None:
            checks[name] = {"status": "failed", "detail": "not configured"}
            return
        await run_check(name, operation())

    await probe("orchestrator", services.orchestrator, lambda: services.orchestrator.list_active_tasks())
    await probe("ledger", services.ledger, lambda: services.ledger.query(LedgerFilters(limit=1)))
    await probe("memory", services.context_store, lambda: services.context_store.read(ContextDocument.SOUL))
    await probe("scheduler", services.job_processor, lambda: services.job_processor.list_jobs(limit=1))

    checks["event_stream"] = (
        {"status": "ok"} if services.stream_manager is not None else {"status": "failed", "detail": "not configured"}
    )

    if services.inference_router is None:
        checks["inference"] = {"status": "failed", "detail": "not configured"}
    else:
        try:
            summary = services.inference_router.health_summary()
            checks["inference"] = {"status": _inference_status(summary), **summary}
        except Exception as exc:
            checks["inference"] = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    return {"status": _overall_status(checks), "checks": checks}


def _inference_status(summary: dict[str, Any]) -> str:
    """ok once models are usable, starting until the first catalogue fetch returns.

    Provider catalogues are fetched over the network *after* the API begins
    serving, so a freshly started North has no models for a few seconds. Calling
    that "failed" cries wolf on every restart. A router that does not report
    ``catalog_loaded`` is treated as loaded, so its behaviour is unchanged.
    """
    if summary.get("ready"):
        return "ok"
    return "failed" if summary.get("catalog_loaded", True) else "starting"


def _overall_status(checks: dict[str, dict[str, Any]]) -> str:
    """ok when everything passed, starting when the only gap is still warming up."""
    statuses = {check["status"] for check in checks.values()}
    if statuses == {"ok"}:
        return "ok"
    if statuses <= {"ok", "starting"}:
        return "starting"
    return "degraded"
