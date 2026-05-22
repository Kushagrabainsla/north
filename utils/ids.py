"""ID generation helpers used by ledger, jobs, and tasks."""

from __future__ import annotations

import uuid

_TASK_ID_LENGTH = 12


def generate_id() -> str:
    """A fresh random ID. 32-char hex from UUID4."""
    return uuid.uuid4().hex


def generate_task_id() -> str:
    """A fresh task ID with a `task_` prefix for human readability in the Ledger."""
    return f"task_{uuid.uuid4().hex[:_TASK_ID_LENGTH]}"
