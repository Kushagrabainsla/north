"""Per-area routers for the orchestrator API (CODING_STYLE §12.4).

Importing a route module registers its routes on the shared `router` from
`deps`, so `app.py` includes one router and each area lives in its own file.
"""

from __future__ import annotations

from orchestrator.api import confidence, health, inference, metrics, stream  # noqa: F401  (registers routes)
from orchestrator.api.deps import configure, health_router, router
from orchestrator.api.health import health_check

__all__ = ["configure", "health_check", "health_router", "router"]
