"""#65: does a Claude Code plan run still run the user's tools after the home folder is closed?

Usage: e65_claude_tools.py <label> <checkout>

One plan run in a throwaway repository runs node, npm, git and python, each printing a marker. The
results hold tool names and yes/no only. On this Mac, node and npm live in a home folder no list names.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

LABEL, CHECKOUT = sys.argv[1], Path(sys.argv[2]).resolve()
sys.path.insert(0, str(CHECKOUT))

from coding_agents import ClaudeBackend, CodingRunner, Mode  # noqa: E402
from tests.unit.coding_agents.conftest import MemoryRecorder  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
CHECKS = {
    "node": "node -e 'console.log(\"NODE-\" + (40 + 2))'",
    "npm": "npm --version && echo NPM-OK",
    "git": "git status --short && echo GIT-OK",
    "python3": "python3 -c 'print(\"PY-\" + str(40 + 2))'",
}
MARKERS = {"node": "NODE-42", "npm": "NPM-OK", "git": "GIT-OK", "python3": "PY-42"}


async def main() -> None:
    folder = Path(tempfile.mkdtemp(prefix="north-e65-tools-")) / "repo"
    folder.mkdir()
    (folder / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
    steps = "\n".join(f"{n}. `{command}`" for n, command in enumerate(CHECKS.values(), 1))
    task = f"Run each of these commands with Bash, one at a time, and quote each one's exact output:\n{steps}"
    runner = CodingRunner({"claude": ClaudeBackend()}, MemoryRecorder())
    report = await runner.run(task_id=f"e65-tools-{uuid.uuid4().hex[:6]}", task=task, workspace=str(folder))
    worked = {name: MARKERS[name] in report.outcome.text for name in CHECKS}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"e65_claude_tools_{LABEL}.json").write_text(json.dumps({"label": LABEL, "worked": worked}) + "\n")
    print(json.dumps(worked, indent=1), "\nmode:", Mode.PLAN.value)


if __name__ == "__main__":
    asyncio.run(main())
