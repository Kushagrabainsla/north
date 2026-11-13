"""A Harbor agent that applies a saved patch to a clean image, so the task's own verifier grades it.

Based on SWE-bench Pro's `patch_replay.PatchReplayAgent`. Imported by Harbor, not by the experiment.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_ENTER = "cd /testbed 2>/dev/null || cd /app"
_APPLY = "git apply --whitespace=nowarn {0} || git apply --3way {0} || patch --fuzz=3 -p1 -i {0}"
# What a shell returns when the command is not there. Terminal-Bench's python and node slim images have neither
# git nor patch, so the patch never reached the code and the verifier graded the code as it was.
_NOT_FOUND = 127
_FILE_PAIR = re.compile(r"^diff --git a/(.+) b/(.+)$", re.M)


def _touched(text: str) -> tuple[list[str], list[str], set[str]]:
    """The paths a patch reads, the paths it writes, and the ones it adds."""
    before, after, added = set(), set(), set()
    for block in re.split(r"(?m)^(?=diff --git )", text):
        pair = _FILE_PAIR.match(block)
        if pair is None:
            continue
        before.add(pair.group(1))
        after.add(pair.group(2))
        if re.search(r"(?m)^new file mode", block.split("\n@@", 1)[0]):
            added.add(pair.group(1))
    return sorted(before), sorted(after), added


async def _apply_outside(environment: BaseEnvironment, patch: Path) -> dict:
    """Apply *patch* on this machine to the files it touches, copied out of the image, then copy them back."""
    where = await environment.exec(command=f"{_ENTER}; pwd", timeout_sec=60)
    root = (where.stdout or "").strip().splitlines()[-1]
    before, after, added = _touched(patch.read_text(errors="replace"))
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        for path in before:
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            if path not in added:
                await environment.download_file(source_path=f"{root}/{path}", target_path=work / path)
        command = ["git", "apply", "--whitespace=nowarn", str(patch.resolve())]
        done = subprocess.run(command, cwd=work, capture_output=True, text=True)
        if done.returncode:
            return {"rc": done.returncode, "out": f"applied outside the image: {done.stdout}{done.stderr}"[-4000:]}
        for path in after:
            if (work / path).is_file():
                await environment.upload_file(source_path=work / path, target_path=f"{root}/{path}")
        gone = [path for path in before if path not in after or not (work / path).exists()]
        if gone:
            await environment.exec(command="rm -f " + " ".join(shlex.quote(f"{root}/{path}") for path in gone))
    return {"rc": 0, "out": f"applied outside the image, which has no git or patch: {len(after)} file(s) in {root}"}


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
        if done.return_code == _NOT_FOUND:
            return await _apply_outside(environment, source)
        return {"rc": done.return_code, "out": ((done.stdout or "") + (done.stderr or ""))[-4000:]}

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        report: dict = {}
        if self._prepare and self._prepare.stat().st_size:
            report["prepare"] = await self._apply(environment, self._prepare, "/tmp/prepare.patch")
        if self._patch.stat().st_size:
            report["patch"] = await self._apply(environment, self._patch, "/tmp/replay.patch")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "replay.json").write_text(json.dumps(report, indent=1))
