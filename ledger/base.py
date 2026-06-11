"""Abstract interface for the Ledger. See docs/CODING_STYLE.md Section 6.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus


@dataclass
class LedgerFilters:
    """Query filters for the ledger. `None` means "no filter on that field"."""

    task_id: str | None = None
    agent: str | None = None
    source: LedgerSource | None = None
    status: LedgerStatus | None = None
    since: datetime | None = None
    limit: int = 100


class LedgerWriter(ABC):
    """Append-only audit trail. Every event in north writes one entry.

    The interface is async even though SQLite is synchronous so that the rest
    of the system can `asyncio.create_task(ledger.write(...))` without blocking
    (docs/CODING_STYLE.md Section 14.1). Concrete implementations off-load the
    blocking I/O via `asyncio.to_thread`.
    """

    @abstractmethod
    async def write(self, entry: LedgerEntry) -> str:
        """Persist one entry. Returns the entry's id.

        Raises:
            LedgerWriteError: if the underlying store rejects the write.
        """

    @abstractmethod
    async def get(self, entry_id: str) -> LedgerEntry | None:
        """Return one entry by id, or None if not found.

        Raises:
            LedgerReadError: if the underlying store fails.
        """

    @abstractmethod
    async def query(self, filters: LedgerFilters) -> list[LedgerEntry]:
        """Return matching entries ordered by timestamp descending.

        Raises:
            LedgerReadError: if the underlying store fails.
        """

    async def pending_task_ids(self, limit: int = 500) -> list[str]:
        """Return task_ids whose *latest* entry is still PENDING.

        Used by the startup reconciliation sweep to find tasks that were
        in-flight when the server stopped. A task that later completed,
        failed, or was cancelled has a newer terminal entry and is excluded.
        Default no-op returns an empty list.
        """
        return []

    async def prune(self, completed_before: datetime, failed_before: datetime) -> int:
        """Delete old terminal entries to keep the ledger bounded.

        Returns the number of rows deleted. Default implementation is a no-op
        so in-memory or read-only stores don't need to override it.
        """
        return 0

    async def cost_breakdown(
        self,
        since: datetime,
        source: LedgerSource | None = None,
        agent: str | None = None,
    ) -> dict:
        """Aggregate cost_usd for entries since *since*.

        Returns {"total": float, "by_component": {agent: cost}, "by_model":
        {model: cost}}. The default implementation aggregates in Python over
        query() so in-memory/fake stores work unchanged; SQL-backed stores
        override it with a GROUP BY, which also avoids the row-limit truncation
        the query() path is subject to.
        """
        entries = await self.query(LedgerFilters(source=source, agent=agent, since=since, limit=10_000))
        total = 0.0
        by_component: dict[str, float] = {}
        by_model: dict[str, float] = {}
        for e in entries:
            cost = e.cost_usd or 0.0
            total += cost
            component = e.agent or "unknown"
            by_component[component] = by_component.get(component, 0.0) + cost
            model = e.model_used or "unknown"
            by_model[model] = by_model.get(model, 0.0) + cost
        return {"total": total, "by_component": by_component, "by_model": by_model}

    async def get_metrics(self, days: int = 7) -> dict:
        """Return aggregated system metrics for the last `days` days.

        Returns a dict with keys: period_days, total_tasks, total_cost_usd,
        total_tokens_in, total_tokens_out, by_agent (list), by_model (dict),
        top_errors (dict). Default no-op returns empty structure.
        """
        return {
            "period_days": days,
            "total_tasks": 0,
            "total_cost_usd": 0.0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "by_agent": [],
            "by_model": {},
            "top_errors": {},
        }
