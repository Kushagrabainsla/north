"""Which coding agents this machine has, and which of them can take a run now."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass

from coding_agents.base import CodingBackend
from coding_agents.claude import ClaudeBackend
from coding_agents.codex import CodexBackend

# The coding agents north can delegate to; a missing one is reported, not hidden.
CODING_AGENT_NAMES = ("codex", "claude")
# Each probe starts the agent's CLI; one that hangs is reported, not waited on.
_PROBE_SECONDS = 30


def discover_backends(*, protected_paths: Sequence[str] = ()) -> dict[str, CodingBackend]:
    """The coding agents installed here, by name. An agent that is not installed is simply absent."""
    found: dict[str, CodingBackend] = {}
    if shutil.which("claude"):
        found["claude"] = ClaudeBackend(protected_paths=protected_paths)
    if shutil.which("codex"):
        found["codex"] = CodexBackend(protected_paths=protected_paths)
    return found


@dataclass(frozen=True)
class Readiness:
    """One coding agent: installed, and whether it can take a run now; if not, what it needs."""

    name: str
    installed: bool
    ready: bool = False
    version: str = ""
    detail: str = ""


async def check_agents(names: Sequence[str] = CODING_AGENT_NAMES) -> list[Readiness]:
    """Each named agent's readiness, by the same probe north runs before it hands an agent any work."""
    backends = discover_backends()

    async def check(name: str) -> Readiness:
        backend = backends.get(name)
        if backend is None:
            return Readiness(name, installed=False, detail=f"`{name}` is not installed")
        try:
            found = await asyncio.wait_for(backend.probe(), _PROBE_SECONDS)
        except TimeoutError:
            return Readiness(name, installed=True, detail="The check timed out")
        return Readiness(name, installed=True, ready=found.available, version=found.version, detail=found.reason)

    return list(await asyncio.gather(*(check(name) for name in names)))
