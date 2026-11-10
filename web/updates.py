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
from typing import Any

from fastapi import APIRouter, HTTPException

from orchestrator.api_context import current_services
from utils.time import epoch_to_local
from utils.update_record import LOG_NAME, last_update, record_update
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


@router.post("/update", status_code=202)
async def start_update() -> dict[str, str]:
    """Start the update. North stops, installs and starts again on its own; the page waits for it."""
    home: Path = current_services().north_home
    last = last_update(home)
    if last is not None and last["state"] == "running":
        raise HTTPException(status_code=409, detail="An update is already running.")
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
    return {"state": "running"}
