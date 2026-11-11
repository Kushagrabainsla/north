"""A Harbor agent that applies a saved patch to a clean image, so the task's own verifier grades it.

Based on SWE-bench Pro's `patch_replay.PatchReplayAgent`. Imported by Harbor, not by the experiment.
"""

from __future__ import annotations

import json
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_ENTER = "cd /testbed 2>/dev/null || cd /app"
_APPLY = "git apply --whitespace=nowarn {0} || git apply --3way {0} || patch --fuzz=3 -p1 -i {0}"


class ReplayAgent(BaseAgent):
    def __init__(self, *args, patch: str, prepare: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._patch = Path(patch)
        self._prepare = Path(prepare) if prepare else None

    @staticmethod
    def name() -> str:
        return "replay-agent"

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def _apply(self, environment: BaseEnvironment, source: Path, target: str) -> dict:
        await environment.upload_file(source_path=source, target_path=target)
        done = await environment.exec(command=f"{_ENTER}; {_APPLY.format(target)}", timeout_sec=300)
        return {"rc": done.return_code, "out": ((done.stdout or "") + (done.stderr or ""))[-4000:]}

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        report: dict = {}
        if self._prepare and self._prepare.stat().st_size:
            report["prepare"] = await self._apply(environment, self._prepare, "/tmp/prepare.patch")
        if self._patch.stat().st_size:
            report["patch"] = await self._apply(environment, self._patch, "/tmp/replay.patch")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "replay.json").write_text(json.dumps(report, indent=1))
