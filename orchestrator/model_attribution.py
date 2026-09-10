"""Ledger-backed model attribution for independent agent execution."""

from __future__ import annotations

import logging

from ledger import LedgerFilters, LedgerWriter

logger = logging.getLogger(__name__)


async def models_used_by(ledger: LedgerWriter, task_id: str, agent_names: set[str]) -> list[str]:
    """Return named agents' completed-task models in stable first-seen order.

    A ledger failure deliberately produces no exclusions: lack of attribution
    must not prevent an otherwise runnable agent from executing.
    """
    if not agent_names:
        return []
    try:
        entries = await ledger.query_summaries(LedgerFilters(task_id=task_id, limit=200))
    except Exception:
        logger.debug("model lookup failed for task %s", task_id, exc_info=True)
        return []

    models: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.action != "agent_completed" or entry.agent not in agent_names:
            continue
        for model in (entry.model_used or "").split(","):
            model = model.strip()
            if model and model not in seen:
                seen.add(model)
                models.append(model)
    return models
