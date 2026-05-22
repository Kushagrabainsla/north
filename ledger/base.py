"""Abstract interface for the Ledger. See docs/CODING_STYLE.md Section 6.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ledger.models import LedgerEntry, LedgerSource


@dataclass
class LedgerFilters:
    """Query filters for the ledger. `None` means "no filter on that field"."""

    task_id: str | None = None
    agent: str | None = None
    source: LedgerSource | None = None
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
