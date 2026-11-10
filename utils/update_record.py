"""How the last update of north went, kept on disk so it outlives the restart the update causes.

`north update` writes it as it goes, whether it was typed in a terminal or started by the
dashboard's Update button. The dashboard reads it once north is back, to say whether the
update worked and, if not, why.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import psutil

RECORD_NAME = "update.json"
# What an update the dashboard started printed. A terminal update prints to the terminal.
LOG_NAME = "update.log"

State = Literal["running", "updated", "failed", "cancelled"]


def record_update(home: Path, state: State, detail: str = "", *, pid: int | None = None) -> None:
    """Note where the update stands now. Written whole, so a reader never sees half a record."""
    record = {"state": state, "detail": detail, "pid": pid or os.getpid(), "at": time.time()}
    home.mkdir(parents=True, exist_ok=True)
    partial = home / f"{RECORD_NAME}.tmp"
    partial.write_text(json.dumps(record), encoding="utf-8")
    partial.replace(home / RECORD_NAME)


def last_update(home: Path) -> dict[str, Any] | None:
    """The last update's record, or None when north was never updated from here.

    An update that says it is running but whose process is gone was stopped before it
    could say how it ended (killed, or the Mac shut down), so it reads as failed.
    """
    record: dict[str, Any] | None = None
    with contextlib.suppress(OSError, ValueError):
        record = json.loads((home / RECORD_NAME).read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("state") not in {"running", "updated", "failed", "cancelled"}:
        return None
    if record["state"] == "running" and not _alive(record.get("pid")):
        record = {**record, "state": "failed", "detail": "The update stopped before it finished."}
    return record


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
