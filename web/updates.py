"""The System page's Update button: install the latest north and restart, from the dashboard.

It runs `north update --yes`, the same update a terminal runs, as its own process: the
update stops this server, so it cannot run inside it. What the update printed goes to
update.log in north's home. How it ended is in update.json (utils.update_record), which
the page reads once north is back.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from orchestrator.api_context import current_services
from utils.time import epoch_to_local
from utils.update_record import LOG_NAME, WAITING, last_update, record_update
from utils.version import NORTH_VERSION, installed_commit

router = APIRouter(tags=["web"])


def _update_command() -> list[str]:
    """`north update --yes`, run by the Python serving now, so it updates this install."""
    return [sys.executable, "-m", "cli.main", "update", "--yes"]


# The folder holding north's packages: site-packages once installed, the checkout in development.
# The update runs from here, since `-m` looks in its working folder first, and nothing in
# another folder (north's home, say) may stand in for north's own `cli`.
_CODE_ROOT = Path(__file__).resolve().parents[1]


@router.get("/update")
async def update_status() -> dict[str, Any]:
    """The version running now, and how the last update went."""
    last = last_update(current_services().north_home)
    if last is not None:
        last = {**last, "at": epoch_to_local(last["at"]).strftime("%b %-d, %-I:%M %p")}
    return {"version": NORTH_VERSION, "commit": installed_commit(), "last": last}


class UpdateRequest(BaseModel):
    """`now` restarts north at once; `idle` waits until north has been idle for ten minutes (issue #84)."""

    model_config = ConfigDict(extra="forbid")

    when: Literal["now", "idle"] = "now"


def begin_update(home: Path) -> None:
    """Start `north update --yes` in a process of its own: it stops this server, so it cannot run inside it."""
    home.mkdir(parents=True, exist_ok=True)
    with open(home / LOG_NAME, "w", encoding="utf-8") as log:
        # A session of its own, so stopping the server does not stop the update with it.
        process = subprocess.Popen(
            _update_command(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=_CODE_ROOT,
            start_new_session=True,
        )
    # Noted now, before the update notes it itself, so a second click cannot start another.
    record_update(home, "running", pid=process.pid)


@router.post("/update", status_code=202)
async def start_update(body: UpdateRequest | None = None) -> dict[str, str]:
    """Update now, or once north is idle. Now: north stops, installs and starts again; the page waits for it.

    When idle: north updates itself once no task, flow run or job has run for ten minutes and
    none is due in the next ten, so no task is cut off by the restart (orchestrator.idle_update).
    """
    home: Path = current_services().north_home
    last = last_update(home)
    if last is not None and last["state"] == "running":
        raise HTTPException(status_code=409, detail="An update is already running.")
    if body is not None and body.when == "idle":
        record_update(home, "waiting", WAITING)
        return {"state": "waiting"}
    begin_update(home)
    return {"state": "running"}


@router.delete("/update")
async def cancel_waiting_update() -> dict[str, str]:
    """Drop an update that waits for north to be idle. One already running cannot be stopped here."""
    home: Path = current_services().north_home
    last = last_update(home)
    if last is None or last["state"] != "waiting":
        raise HTTPException(status_code=409, detail="No update is waiting.")
    record_update(home, "cancelled", "You cancelled the update before it started.")
    return {"state": "cancelled"}
