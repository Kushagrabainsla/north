"""Ledger queries and full-text search."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from ledger.base import LedgerFilters
from ledger.models import LedgerEntry, LedgerSource
from orchestrator.api.deps import _get_ledger, router

# Bulk fields the ledger listing never needs. frozenset so a caller cannot
# mutate the exclusion set that every /ledger response is rendered through.
_LEDGER_EXCLUDE: frozenset[str] = frozenset({"agent_output", "tools_used"})


@router.get("/ledger", response_model=list[LedgerEntry], response_model_exclude=_LEDGER_EXCLUDE)
async def query_ledger(
    task_id: str | None = None,
    run_id: str | None = None,
    agent: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[LedgerEntry]:
    """Query ledger entries with optional filters."""
    src: LedgerSource | None = None
    if source is not None:
        try:
            src = LedgerSource(source)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown source {source!r}. Valid: {[s.value for s in LedgerSource]}",
            ) from None
    return await _get_ledger().query(
        LedgerFilters(task_id=task_id, run_id=run_id, agent=agent, source=src, limit=limit)
    )


class SearchOut(BaseModel):
    entry: LedgerEntry
    rank: float
    snippet: str


@router.get("/ledger/search", response_model=list[SearchOut])
async def search_ledger(
    q: str,
    limit: int = 20,
    agent: str | None = None,
    source: str | None = None,
) -> list[SearchOut]:
    """Full-text search over ledger entries."""
    src: LedgerSource | None = None
    if source is not None:
        try:
            src = LedgerSource(source)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown source {source!r}. Valid: {[s.value for s in LedgerSource]}",
            ) from None
    results = await _get_ledger().search(query=q, limit=limit, agent=agent, source=src)
    return [SearchOut(entry=r.entry, rank=r.rank, snippet=r.snippet) for r in results]


