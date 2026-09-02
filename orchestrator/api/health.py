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
    directly. It returns HTTP 200 with status=degraded when the API is alive but a
    dependency is not ready, letting the browser tell that apart from an
    unreachable API.
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
            checks["inference"] = {"status": "ok" if summary["ready"] else "failed", **summary}
        except Exception as exc:
            checks["inference"] = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    status = "ok" if all(check["status"] == "ok" for check in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
