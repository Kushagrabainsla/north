"""Docker-backed command sandbox for the bash tool (#6 sandboxed execution).

Defense in depth for agent-run shell commands: even an *approved* command can be
confined to a container that only sees the workspace (mounted at ``/workspace``),
with the network off and memory/CPU/PID limits applied - so a mistake or a
misbehaving model can't reach the rest of the host or the internet.

Opt-in via settings (``sandbox_enabled``) and **fail-closed**: when sandboxing is
requested but Docker isn't available, the command is refused rather than silently
run on the host - a security control must never degrade to "no protection".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DOCKER_CHECK_TIMEOUT = 10
_CONTAINER_WORKDIR = "/workspace"


@dataclass(frozen=True)
class SandboxConfig:
    """How to sandbox a command. ``enabled=False`` means run normally on the host."""

    enabled: bool = False
    image: str = "python:3.12-slim"
    network_disabled: bool = True
    memory: str = "512m"
    cpus: str = "1"
    pids_limit: int = 512

    @classmethod
    def from_settings(cls, settings: object) -> SandboxConfig:
        get = lambda name, default: getattr(settings, name, default)  # noqa: E731
        return cls(
            enabled=bool(get("sandbox_enabled", False)),
            image=str(get("sandbox_image", cls.image)),
            network_disabled=bool(get("sandbox_network_disabled", True)),
            memory=str(get("sandbox_memory", cls.memory)),
            cpus=str(get("sandbox_cpus", cls.cpus)),
            pids_limit=int(get("sandbox_pids_limit", cls.pids_limit)),
        )


def build_run_argv(command: str, workspace: str, cfg: SandboxConfig) -> list[str]:
    """Construct the ``docker run`` argv that executes *command* inside a container.

    The workspace is bind-mounted read-write at /workspace and becomes the working
    directory; the command runs via ``sh -c`` so shell syntax still works.
    """
    ws = str(Path(workspace).resolve())
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        cfg.memory,
        "--cpus",
        cfg.cpus,
        "--pids-limit",
        str(cfg.pids_limit),
    ]
    if cfg.network_disabled:
        argv += ["--network", "none"]
    argv += [
        "-v",
        f"{ws}:{_CONTAINER_WORKDIR}",
        "-w",
        _CONTAINER_WORKDIR,
        cfg.image,
        "sh",
        "-c",
        command,
    ]
    return argv


async def docker_available() -> bool:
    """True when a Docker daemon is reachable (``docker info`` exits 0)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await asyncio.wait_for(proc.wait(), timeout=_DOCKER_CHECK_TIMEOUT)
    except (FileNotFoundError, OSError):
        return False
    except TimeoutError:
        with_suppressed_kill(proc)
        return False
    return code == 0


def with_suppressed_kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
