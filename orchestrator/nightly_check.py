"""The nightly check - lint, the whole suite, the live coding-agent tests - run by north on itself.

launchd used to start scripts/smoke.sh at 03:30. macOS keeps background jobs out
of the Desktop, Documents and Downloads folders, and north's project sat on the
Desktop: from Oct 7 every night ended "Operation not permitted" before the
script began, so the failure it exists to report was never reported. The north
server can read the folder it was started from, so it runs the script itself.

The live stage drives the installed Claude Code and Codex, the agents north
hands its coding to. They update often, and an update that changes their flags
or output breaks north's coding where no test with a fake can see it.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from config.settings import settings
from inference.exceptions import WaitingFor
from utils.power import fully_awake

# Written by scripts/install_smoke_schedule.sh, run from the project folder:
# north can live anywhere, and only a machine with its project folder can check it.
CHECKOUT_FILE_NAME = "checkout"
_TIME_LIMIT_S = 60 * 60


def checkout_file() -> Path:
    return settings.north_home / "smoke" / CHECKOUT_FILE_NAME


def registered_checkout() -> Path | None:
    """The project folder the nightly check runs in, or None when none is registered."""
    try:
        folder = Path(checkout_file().read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        return None
    return folder if (folder / "scripts" / "smoke.sh").is_file() else None


async def nightly_check() -> str:
    """Run scripts/smoke.sh with the live tests; the summary on a pass, an error naming what failed otherwise."""
    folder = registered_checkout()
    if folder is None:
        raise RuntimeError(
            "No north project folder is registered for the nightly check. "
            "Run scripts/install_smoke_schedule.sh from the project folder."
        )
    # A closed laptop wakes for seconds at a time; the check takes minutes and
    # would be cut off. It waits for a real wake instead, and runs then.
    if not fully_awake():
        raise WaitingFor("an awake laptop", "The laptop is asleep; the nightly check runs as soon as it is awake.")
    # The lowest CPU priority: after a night asleep it can run while you work.
    process = await asyncio.create_subprocess_exec(
        "nice",
        "-n",
        "19",
        str(folder / "scripts" / "smoke.sh"),
        cwd=folder,
        env={**os.environ, "SMOKE_LIVE": "1", "NORTH_HOME": str(settings.north_home)},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group, so a time-out stops the tests it started
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), _TIME_LIMIT_S)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise RuntimeError(f"The nightly check did not finish within {_TIME_LIMIT_S // 60} minutes.") from None
    last = next((line for line in reversed(output.decode(errors="replace").splitlines()) if line.strip()), "")
    if process.returncode != 0:
        raise RuntimeError(f"The nightly check {last.removeprefix('smoke ') or 'failed'}")
    return f"The nightly check {last.removeprefix('smoke ') or 'passed'}"
