"""One delegated coding run, start to finish: pick an agent, record it, run it, record how it ended."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from coding_agents.base import CodingBackend, RunRecorder, RunStart
from coding_agents.exceptions import BackendUnavailableError
from coding_agents.models import EventKind, FailureKind, Mode, RunEvent, RunOutcome, RunSpec
from utils.ids import generate_id

logger = logging.getLogger(__name__)

AGENT_PREFIX = "coding:"


@dataclass(frozen=True)
class RunReport:
    run_id: str
    backend: str
    outcome: RunOutcome


class CodingRunner:
    """Runs a task on an installed coding agent, and leaves the run on the record."""

    def __init__(self, backends: Mapping[str, CodingBackend], recorder: RunRecorder) -> None:
        self._backends = dict(backends)
        self._recorder = recorder

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._backends)

    async def run(
        self,
        *,
        task_id: str,
        task: str,
        workspace: str,
        guidance: str = "",
        backend: str | None = None,
        mode: Mode = Mode.PLAN,
    ) -> RunReport:
        """Run *task* in *workspace*; a re-planned task that finds its own live run continues that session."""
        chosen, version = await self._choose(backend)
        agent = AGENT_PREFIX + chosen.name
        live = await self._recorder.live_run(task_id, agent)
        run_id = live.run_id if live else generate_id()
        spec = RunSpec(
            task=task,
            workspace=workspace,
            session_id=live.session_id if live else str(uuid.uuid4()),
            mode=mode,
            resume=live is not None,
            guidance=guidance,
        )
        # Written before the process starts, so a crash can still find the session.
        await self._recorder.start(RunStart(run_id, task_id, agent, task, workspace))
        await self._recorder.remember(
            run_id,
            {
                "provider": chosen.provider,
                "session_id": spec.session_id,
                "cli_version": version,
                "workspace": workspace,
                "mode": mode.value,
                "resumed": spec.resume,
            },
        )
        outcome = await self._execute(chosen, spec, run_id, task_id)
        await self._recorder.finish(run_id, outcome)
        return RunReport(run_id, chosen.name, outcome)

    async def _choose(self, wanted: str | None) -> tuple[CodingBackend, str]:
        """The backend asked for, or the first that can take a run; why none can, when none can."""
        if wanted is not None and wanted not in self._backends:
            raise BackendUnavailableError(f"{wanted!r} is not a coding agent north can run ({', '.join(self.names)})")
        reasons = []
        for name in [wanted] if wanted else self.names:
            availability = await self._backends[name].probe()
            if availability.available:
                return self._backends[name], availability.version
            reasons.append(f"{name}: {availability.reason}")
        raise BackendUnavailableError("; ".join(reasons) or "no coding agent is installed")

    async def _execute(self, backend: CodingBackend, spec: RunSpec, run_id: str, task_id: str) -> RunOutcome:
        async def sink(event: RunEvent) -> None:
            try:
                if event.kind is EventKind.STARTED:
                    await self._recorder.remember(run_id, {"provider": backend.provider, **event.data})
                await self._recorder.record(run_id, task_id, event.kind.value, event.data)
            except Exception:
                logger.warning("could not record %s for coding run %s", event.kind, run_id, exc_info=True)

        try:
            return await backend.run(spec, sink)
        except asyncio.CancelledError:
            cancelled = RunOutcome(False, "", spec.session_id, failure=FailureKind.CANCELLED, error="Run cancelled.")
            await asyncio.shield(self._recorder.finish(run_id, cancelled))
            raise
        except Exception as exc:
            logger.exception("coding run %s failed unexpectedly", run_id)
            return RunOutcome(
                False, "", spec.session_id, failure=FailureKind.ERROR, error=f"{type(exc).__name__}: {exc}"
            )
