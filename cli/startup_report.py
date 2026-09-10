"""Best-effort extraction of the exception a failed server start wrote to its log."""

from __future__ import annotations

import re
from pathlib import Path

# The last line of a Python traceback: "TypeError: configure() got an ...".
# That line is the answer; the frames above it are context.
EXCEPTION_LINE = re.compile(r"^\s*(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt)\b\s*:")


def last_error_lines(log: Path, limit: int = 3) -> list[str]:
    """Return the trailing exception context, or the last lines when none is found.

    Structured JSON records are skipped, and any read failure yields no lines:
    this runs while another failure is being reported and must never raise.
    """
    try:
        raw = log.read_text(errors="replace").splitlines()
    except Exception:
        return []
    lines = [line.rstrip() for line in raw[-400:] if line.strip() and not line.lstrip().startswith("{")]
    for index in range(len(lines) - 1, -1, -1):
        if EXCEPTION_LINE.match(lines[index]):
            return lines[max(0, index - limit + 1) : index + 1]
    return lines[-limit:]
