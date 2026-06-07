"""Task context snapshots for cross-agent continuity."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from config.settings import settings


@dataclass
class TaskContextSnapshot:
    """Immutable snapshot of task state for cross-agent handoffs."""

    task_id: str
    original_request: str
    branch: str = ""
    stage: str = ""
    spec_path: str = ""
    implementation_notes_path: str = ""
    qa_report_path: str = ""
    files_changed: list[str] = field(default_factory=list)
    last_test_status: str = ""
    failure_count: int = 0
    agents_visited: list[str] = field(default_factory=list)
    next_agent: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert snapshot to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskContextSnapshot:
        """Construct snapshot from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class TaskContextSnapshotStore:
    """Persistent store for task context snapshots."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.snapshot_path = settings.north_home / "tasks" / task_id / "context_snapshot.json"

    async def write(self, snapshot: TaskContextSnapshot) -> None:
        """Write snapshot to disk (fire-and-forget)."""
        snapshot.updated_at = datetime.utcnow().isoformat()
        if not snapshot.created_at:
            snapshot.created_at = snapshot.updated_at

        def _write_sync() -> None:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_path.write_text(
                json.dumps(snapshot.to_dict(), indent=2),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write_sync)

    async def read(self) -> TaskContextSnapshot | None:
        """Read snapshot from disk, or None if not found."""

        def _read_sync() -> TaskContextSnapshot | None:
            if not self.snapshot_path.exists():
                return None
            try:
                data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                return TaskContextSnapshot.from_dict(data)
            except (json.JSONDecodeError, OSError):
                return None

        return await asyncio.to_thread(_read_sync)

    def read_sync(self) -> TaskContextSnapshot | None:
        """Synchronous read (for initialization paths)."""
        if not self.snapshot_path.exists():
            return None
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            return TaskContextSnapshot.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None
