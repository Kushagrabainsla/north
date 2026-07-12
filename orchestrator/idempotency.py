"""In-memory idempotency cache for task submissions.

Collapses duplicate submissions - the classic case being a webhook delivered
more than once - into a single task within a short window. Best-effort and
process-local: a restart clears it, which is fine because the window is small and
interrupted tasks are recovered separately.
"""

from __future__ import annotations

import hashlib
import time

from orchestrator.models import TaskRequest


def idempotency_key(request: TaskRequest) -> str:
    """Return the dedup key for *request*: its explicit key, else a source+prompt hash."""
    if request.idempotency_key:
        return request.idempotency_key
    digest = hashlib.sha256(
        f"{request.source.value}\x00{request.forced_agent or ''}\x00{request.prompt}".encode()
    ).hexdigest()
    return f"auto:{digest}"


class IdempotencyCache:
    """Maps a recent idempotency key to the task_id it produced, with a TTL."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        """Return the task_id previously recorded for *key* if still within the TTL."""
        self._evict()
        entry = self._entries.get(key)
        return entry[0] if entry is not None else None

    def put(self, key: str, task_id: str) -> None:
        self._entries[key] = (task_id, time.monotonic())

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, ts) in self._entries.items() if now - ts > self._ttl]
        for k in expired:
            del self._entries[k]
