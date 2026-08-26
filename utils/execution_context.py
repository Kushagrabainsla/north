"""Task-local execution identity shared by agents, ledger, and event streams.

Context variables keep run metadata attached across async calls without adding
``run_id`` arguments to every helper.  They are safe across concurrently running
agents because each asyncio task receives its own context.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionIdentity:
    run_id: str
    parent_run_id: str | None = None
    attempt: int = 0


_CURRENT_EXECUTION: ContextVar[ExecutionIdentity | None] = ContextVar("north_execution", default=None)


def current_execution() -> ExecutionIdentity | None:
    """Return the current agent-run identity, if execution is inside an agent."""
    return _CURRENT_EXECUTION.get()


@contextmanager
def bind_execution(identity: ExecutionIdentity) -> Iterator[None]:
    """Bind *identity* for the duration of one agent invocation."""
    token = _CURRENT_EXECUTION.set(identity)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)
