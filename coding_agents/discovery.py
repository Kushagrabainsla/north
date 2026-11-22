"""Which coding agents this machine has, and which of them can take a run now."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from coding_agents.base import CodingBackend
from coding_agents.claude import ClaudeBackend
from coding_agents.codex import CodexBackend
from coding_agents.models import FailureKind, Mode, RunEvent, RunSpec

# The coding agents north can delegate to; a missing one is reported, not hidden.
CODING_AGENT_NAMES = ("codex", "claude")
# Each probe starts the agent's CLI; one that hangs is reported, not waited on.
_PROBE_SECONDS = 30
# A one-line request proves what a probe cannot: that the account still has credit.
# On 2026-11-18 Codex probed "ready" and every run said "Your workspace is out of credits".
_TRIAL_TASK = "Reply with the single word ready. Use no tools."
_TRIAL_SECONDS = 120
_TRIAL_TURNS = 2
_TRIAL_BUDGET_USD = 0.05

# What to do about a failed one-line request, by why it failed. The agent's own error says what happened.
_FIXES: dict[FailureKind | None, str] = {
    FailureKind.RESOURCE: "Wait for {name}'s usage limit to reset, or add credit to its account.",
    FailureKind.AUTH: "Log in to {name} again in a terminal.",
    FailureKind.CONFIG: "Check the model and settings {name} is set to use.",
}
_FIX_OTHERWISE = "Run `{name}` once in a terminal and read the error it shows."


@dataclass(frozen=True)
class Readiness:
    """One coding agent: installed, and whether it can take a run now; if not, what it needs.

    *detail* is what went wrong, in words a person can act on. *fix* is what to do, when it is not
    already in *detail* (a probe's reason names its own command, as in "run `claude auth login`").
    """

    name: str
    installed: bool
    ready: bool = False
    version: str = ""
    detail: str = ""
    fix: str = ""


def discover_backends(*, protected_paths: Sequence[str] = ()) -> dict[str, CodingBackend]:
    """The coding agents installed here, by name. An agent that is not installed is simply absent."""
    found: dict[str, CodingBackend] = {}
    if shutil.which("claude"):
        found["claude"] = ClaudeBackend(protected_paths=protected_paths)
    if shutil.which("codex"):
        found["codex"] = CodexBackend(protected_paths=protected_paths)
    return found


async def check_agents(names: Sequence[str] = CODING_AGENT_NAMES) -> list[Readiness]:
    """Each named agent's readiness, by the same probe north runs before it hands an agent any work."""
    backends = discover_backends()
    return list(await asyncio.gather(*(_probe(name, backends.get(name)) for name in names)))


async def try_agents(names: Sequence[str] = CODING_AGENT_NAMES) -> list[Readiness]:
    """Each named agent's readiness, proven by a one-line request to each agent the probe passes.

    It spends a few tokens of each plan. The nightly check runs it before its live tests, so an
    agent that is logged out or out of credit is named with its fix, and its tests are not run.
    """
    backends = discover_backends()

    async def check(name: str) -> Readiness:
        backend = backends.get(name)
        found = await _probe(name, backend)
        if not found.ready or backend is None:
            return found
        return await _trial(backend, found)

    return list(await asyncio.gather(*(check(name) for name in names)))


async def _probe(name: str, backend: CodingBackend | None) -> Readiness:
    if backend is None:
        return Readiness(name, installed=False, detail=f"`{name}` is not installed")
    try:
        found = await asyncio.wait_for(backend.probe(), _PROBE_SECONDS)
    except TimeoutError:
        return Readiness(name, installed=True, detail="The check timed out", fix=_FIX_OTHERWISE.format(name=name))
    return Readiness(name, installed=True, ready=found.available, version=found.version, detail=found.reason)


async def _trial(backend: CodingBackend, found: Readiness) -> Readiness:
    """*found*, or why the agent could not answer a one-line request and what to do about it."""

    async def ignore(_event: RunEvent) -> None:
        return None

    with tempfile.TemporaryDirectory(prefix="north-trial-") as folder:
        await _git_init(folder)
        spec = RunSpec(
            task=_TRIAL_TASK,
            workspace=folder,
            session_id=str(uuid.uuid4()),
            mode=Mode.PLAN,
            max_turns=_TRIAL_TURNS,
            max_budget_usd=_TRIAL_BUDGET_USD,
        )
        try:
            outcome = await asyncio.wait_for(backend.run(spec, ignore), _TRIAL_SECONDS)
        except TimeoutError:
            return replace(
                found,
                ready=False,
                detail=f"{found.name} did not answer a one-line request in {_TRIAL_SECONDS} seconds",
                fix=_FIX_OTHERWISE.format(name=found.name),
            )
    if outcome.ok:
        return found
    fix = _FIXES.get(outcome.failure, _FIX_OTHERWISE).format(name=found.name)
    error = (outcome.error or "no reason given").strip().rstrip(".")
    return replace(found, ready=False, detail=f"{found.name} could not answer a one-line request: {error}", fix=fix)


async def _git_init(folder: str) -> None:
    """Make *folder* a repository: an agent may refuse to work outside one."""
    process = await asyncio.create_subprocess_exec(
        "git", "init", "-q", folder, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()
