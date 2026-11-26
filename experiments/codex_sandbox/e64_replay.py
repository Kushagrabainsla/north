"""#64 before/after: do the sandbox refusals of north's Codex runs reach north's record of the run?

Usage: e64_replay.py <label> <checkout>

Reads every Codex session north started since the home folder was closed (2026-10-03) whose folder is
in north's copies folder (`~/.cache/north`): the benchmark, live tests, smoke runs, the nightly check
and the user's own tasks, all on north's real profile. Unit-test runs in temp folders are left out.
Each finished command goes through *checkout*'s own `_Run`, as the app-server would send it, and the
run's denials are counted.
Read-only. The results hold counts only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LABEL, CHECKOUT = sys.argv[1], Path(sys.argv[2]).resolve()
sys.path.insert(0, str(CHECKOUT))

from coding_agents.codex import _Run  # noqa: E402

HOME = Path.home()
SESSIONS = HOME / ".codex" / "sessions"
SINCE = 1759478400  # 2026-10-03, when #61 closed the home folder
MARKERS = ("operation not permitted", "read-only file system")
RESULTS = Path(__file__).resolve().parent / "results"


def as_app_server(item: dict) -> dict:
    """A rollout's command item, in the shape the app-server sends north."""
    exit_code = item.get("exit_code")
    status = "failed" if exit_code not in (0, None) else "completed"
    return {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "commandExecution",
                "id": item.get("id"),
                "status": status,
                "exitCode": exit_code,
                "aggregatedOutput": item.get("aggregated_output") or "",
            }
        },
    }


def main() -> None:
    runs = commands = failed_by_refusal = runs_with_one = recorded = runs_recorded = 0
    for path in sorted(SESSIONS.rglob("*.jsonl")):
        if path.stat().st_mtime < SINCE:
            continue
        lines = path.read_text(errors="replace").splitlines()
        if not lines:
            continue
        meta = json.loads(lines[0]).get("payload", {})
        if meta.get("originator") != "north" or not meta.get("cwd", "").startswith(str(HOME / ".cache" / "north")):
            continue
        runs += 1
        run = _Run("replay")
        had = False
        for line in lines[1:]:
            if '"CommandExecution"' not in line:
                continue
            payload = json.loads(line).get("payload") or {}
            item = payload.get("item") or {}
            if payload.get("type") != "item_completed" or item.get("type") != "CommandExecution":
                continue
            commands += 1
            output = str(item.get("aggregated_output") or "").lower()
            if item.get("exit_code") not in (0, None) and any(m in output for m in MARKERS):
                failed_by_refusal += 1
                had = True
            run.read(as_app_server(item))
        runs_with_one += had
        recorded += len(run.denials)
        runs_recorded += bool(run.denials)
    result = {
        "label": LABEL,
        "real_runs": runs,
        "commands": commands,
        "commands_failed_with_a_refusal": failed_by_refusal,
        "runs_with_such_a_command": runs_with_one,
        "refusals_recorded": recorded,
        "runs_with_a_recorded_refusal": runs_recorded,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"e64_replay_{LABEL}.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
