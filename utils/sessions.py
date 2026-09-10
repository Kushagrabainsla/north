"""Shared read contract for discovering currently active task sessions."""

from __future__ import annotations

from typing import Any, Protocol


class ActiveSessionStorePort(Protocol):
    """Read-only active-session query required by GetActiveSessionsTool."""

    async def list_active(self, exclude_task_id: str | None = None) -> list[dict[str, Any]]: ...
