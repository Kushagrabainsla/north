"""Per-area routers for the orchestrator API (CODING_STYLE §12.4).

Each area owns a module; importing it registers its routes on the shared
``router`` (or its own, for the unauthenticated health and webhook routers).
``app.py`` includes the three routers exported here and calls ``configure``.
"""

from __future__ import annotations

from orchestrator.api import (  # noqa: F401  (importing registers each area's routes)
    agents,
    approval,
    confidence,
    context,
    cron,
    health,
    inference,
    jobs,
    ledger,
    metrics,
    runs,
    settings,
    stream,
    task,
    transcription,
)
from orchestrator.api.deps import configure, health_router, router
from orchestrator.api.health import health_check
from orchestrator.api.webhooks import webhook_router

__all__ = ["configure", "health_check", "health_router", "router", "webhook_router"]
