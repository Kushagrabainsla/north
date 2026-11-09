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
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.secrets import blank_query_values, redact, redact_for_logging

# ---------------------------------------------------------------------------
# Context variable - holds the active task_id for the current async context.
# ---------------------------------------------------------------------------

_task_id_var: ContextVar[str] = ContextVar("task_id", default="")
_CHATTY_LIBRARIES = ("httpx", "httpcore", "aiogram.event")

# Set by `north start`, which leaves the server to write north.log itself so it can rotate it.
LOG_TO_HOME_ENV = "NORTH_LOG_TO_HOME"
_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUPS = 5
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def bind_task_id(task_id: str) -> None:
    """Set the active task_id in the current async context."""
    _task_id_var.set(task_id)


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

    def __init__(self, wrapped: logging.Formatter, hide: Callable[[str], str] = str) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._hide = hide

    def format(self, record: logging.LogRecord) -> str:
        return self._hide(blank_query_values(redact(self._wrapped.format(record))))


def _redact_existing_handlers(hide: Callable[[str], str] = str) -> None:
    """Wrap handlers already installed by servers or libraries."""
    loggers = [logging.getLogger()]
    loggers.extend(logger for logger in logging.root.manager.loggerDict.values() if isinstance(logger, logging.Logger))
    seen: set[int] = set()
    for logger in loggers:
        for handler in logger.handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            formatter = handler.formatter or logging.Formatter()
            if not isinstance(formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(formatter, hide))


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def configure_structured_logging(
    level: int = logging.INFO, *, hide: Callable[[str], str] = str, log_file: Path | None = None
) -> None:
    """Install the JSON formatter on the root logger.

    Call this once at application startup (``orchestrator/app.py``) before
    uvicorn sets up its own handlers.  Uvicorn's own log records flow through
    the root logger and are therefore also JSON-formatted.

    When NORTH_LOG_FILE is set, logs go to that file instead of stdout so the
    interactive chat REPL isn't polluted by server output.

    *hide* takes known credentials out of each line; the caller supplies it, because
    only the app knows where they are kept.

    With *log_file* the log rotates there, uvicorn's lines included.
    """

    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers[:]:
        root.removeHandler(h)

    handler: logging.Handler
    if log_file is None:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = RotatingFileHandler(log_file, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
        for name in _UVICORN_LOGGERS:
            logging.getLogger(name).handlers.clear()
            logging.getLogger(name).propagate = True
    handler.setFormatter(_RedactingFormatter(_JSONFormatter(), hide))
    root.addHandler(handler)
    _redact_existing_handlers(hide)
    # These log every request at INFO, httpx with its full URL, where a tool's key can sit.
    for name in _CHATTY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
