"""System performance metrics."""

from __future__ import annotations

from orchestrator.api.deps import _get_ledger, router


@router.get("/metrics")
async def get_metrics(days: int = 7) -> dict:
    """Return aggregated system performance metrics.

    Query params:
        days: look-back window in days (default 7; max 365)
    """
    days = max(1, min(days, 365))
    return await _get_ledger().get_metrics(days=days)


