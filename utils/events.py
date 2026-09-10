"""Platform-level structural contract for publishing task-scoped events.

`EventEmitter` is the one shape lower layers need in order to surface live
progress: a single async ``emit(task_id, event, data)`` call. It lives in the
platform layer so integrations (tools), intelligence, and application code can
all type against it without importing the concrete ``EventStreamManager`` from
``orchestrator`` - an outward, layer-violating dependency.

The concrete ``orchestrator.stream.EventStreamManager`` satisfies this protocol
structurally; no registration or subclassing is required.

See docs/CODING_STYLE.md Section 15 and architecture/modules.yaml (platform layer).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventEmitter(Protocol):
    """Anything that can publish a task-scoped event to connected subscribers."""

    async def emit(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """Publish ``event`` (with ``data``) to subscribers following ``task_id``."""
        ...
