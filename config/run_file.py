"""Where the running north server listens, for the CLI started with the same home.

The CLI assumed port 8000. In the issue #76 test a north started on 8300 was
unreachable by every CLI command ("Cannot reach the north server"), `north
status` said "server down" and exited 0, and when another local user listened
on 8000 the CLI sent them the master secret and showed their empty answers as
fact. The server now writes its address here once it is listening; the CLI
reads it, and checks the server is really this home's north before sending the
secret (`config.security.server_proof`).

Readable only by its owner, in a folder only its owner can enter, and ignored
once the process it names has exited.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunningServer:
    url: str
    pid: int


def _path() -> Path:
    return settings.north_home / "run" / "server.json"


def write_run_file(url: str, pid: int | None = None) -> None:
    """Record that this process serves *url*."""
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    partial = path.with_suffix(".tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "pid": pid or os.getpid()}, handle)
    partial.replace(path)


def read_run_file() -> RunningServer | None:
    """The server this home's north is running, or None when none is."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        server = RunningServer(url=str(data["url"]).rstrip("/"), pid=int(data["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return server if psutil.pid_exists(server.pid) else None


def remove_run_file(pid: int | None = None) -> None:
    """Forget the server, if the file still names this process (a newer server may have replaced it)."""
    path = _path()
    try:
        if int(json.loads(path.read_text(encoding="utf-8"))["pid"]) == (pid or os.getpid()):
            path.unlink()
    except (OSError, ValueError, KeyError, TypeError):
        logger.debug("no run file of ours to remove", exc_info=True)
