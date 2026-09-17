"""Structured logging with task_id correlation IDs.

Every log record emitted while a task is being processed is automatically
enriched with the active ``task_id`` from a ``contextvars.ContextVar``.  Call
``bind_task_id(task_id)`` at the top of any async code path that belongs to a
specific task (e.g. ``_process_task``).

Usage::

    from utils.logging import bind_task_id
    import logging

    log = logging.getLogger(__name__)

    async def _process_task(task_id, ...):
        bind_task_id(task_id)
        log.info("starting pipeline")          # → {"task_id": "task_…", …}
        log.info("done", extra={"agent": "x"}) # → {"task_id": "…", "agent": "x", …}

See docs/CODING_STYLE.md Section 16.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from utils.secrets import redact, redact_for_logging

# ---------------------------------------------------------------------------
# Context variable - holds the active task_id for the current async context.
# ---------------------------------------------------------------------------

_task_id_var: ContextVar[str] = ContextVar("task_id", default="")


def bind_task_id(task_id: str) -> None:
    """Set the active task_id in the current async context."""
    _task_id_var.set(task_id)


def current_task_id() -> str:
    """Return the task_id bound to the current async context, or empty string."""
    return _task_id_var.get()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record, always including ``task_id``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }

        task_id = _task_id_var.get()
        if task_id:
            payload["task_id"] = task_id

        # Merge any extra fields passed via logging.info(..., extra={...})
        _SKIP = logging.LogRecord.__dict__.keys() | {
            "message",
            "asctime",
            "args",
            "msg",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        }
        for key, val in record.__dict__.items():
            if key not in _SKIP:
                payload[key] = redact_for_logging(val, key=key)

        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))

        # One last pass catches credentials embedded by an object's __str__.
        return redact(json.dumps(payload, default=str))


class _RedactingFormatter(logging.Formatter):
    """Sanitise output produced by a third-party formatter.

    Uvicorn installs handlers before FastAPI enters its lifespan. Wrapping those
    existing formatters closes the gap without replacing their presentation.
    """

    def __init__(self, wrapped: logging.Formatter) -> None:
        super().__init__()
        self._wrapped = wrapped

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._wrapped.format(record))


def _redact_existing_handlers() -> None:
    """Wrap handlers already installed by servers or libraries."""
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    seen: set[int] = set()
    for logger in loggers:
        for handler in logger.handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            formatter = handler.formatter or logging.Formatter()
            if not isinstance(formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(formatter))


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Call this once at application startup (``orchestrator/app.py``) before
    uvicorn sets up its own handlers.  Uvicorn's own log records flow through
    the root logger and are therefore also JSON-formatted.

    When NORTH_LOG_FILE is set, logs go to that file instead of stdout so the
    interactive chat REPL isn't polluted by server output.
    """

    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RedactingFormatter(_JSONFormatter()))
    root.addHandler(handler)
    _redact_existing_handlers()
