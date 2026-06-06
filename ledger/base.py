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

    async def prune(self, completed_before: datetime, failed_before: datetime) -> int:
        """Delete old terminal entries to keep the ledger bounded.

        Returns the number of rows deleted. Default implementation is a no-op
        so in-memory or read-only stores don't need to override it.
        """
        return 0

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
