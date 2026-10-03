"""Run the same tasks through each arm and grade them identically.

    python experiments/coding_paths_comparison/compare.py --arms raw_claude,raw_codex,new_claude,new_codex

Grading is the held-out test of each task, run on a copy of what the arm left. For the new path, a change that
north did not apply is graded twice: as landed (the working tree) and in its kept branch, so what the agent
could do is told apart from what north's policy let through.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2]))

import arms as arm_runners  # noqa: E402
from workspace import grade, inspect, load_tasks, own_tests_pass, prepare  # noqa: E402

RESULTS = Path(__file__).parent / "results"


def build_arms(args: argparse.Namespace) -> dict:
    return {
        "raw_claude": arm_runners.raw_claude,
        "raw_codex": arm_runners.raw_codex,
        "new_claude": arm_runners.new_path("claude"),
        "new_codex": arm_runners.new_path("codex"),
    }


def watch_workspace(path: Path, started: float, samples: list, stop: threading.Event) -> None:
    """Note, each second, what is changed in the workspace, so a run's edits and landing have a time."""
    last: str | None = None
    while not stop.is_set():
        done = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True)
        if done.stdout != last:
            last = done.stdout
            samples.append(
                {"t": round(time.monotonic() - started, 1), "changed": sorted(line[3:] for line in last.splitlines())}
            )
        stop.wait(1.0)


def run_one(arm_name: str, runner, task, out: Path, work: Path) -> dict:
    prepared = prepare(task, work / f"{arm_name}-{task.id}")
    print(f"  {arm_name:13} {task.id} ...", end="", flush=True)
    started = time.monotonic()
    samples: list = []
    stop = threading.Event()
    threading.Thread(target=watch_workspace, args=(prepared.path, started, samples, stop), daemon=True).start()
    try:
        result = runner(task, prepared, out / f"{arm_name}__{task.id}.log")
        crashed = ""
    except Exception as exc:  # one arm failing must not stop the rest
        result = arm_runners.ArmResult(arm_name, error=f"{type(exc).__name__}: {exc}")
        crashed = result.error
    stop.set()
    passed, grader_tail = grade(task, prepared.path)
    record = {
        "arm": arm_name,
        "task": task.id,
        "level": task.level,
        "passed_as_landed": passed,
        "grader_tail": grader_tail if not passed else "",
        "own_tests_pass": own_tests_pass(task, prepared.path),
        "wall_seconds": round(time.monotonic() - started, 1),
        **{k: v for k, v in asdict(result).items() if k not in ("branch_path",)},
        **inspect(prepared, prepared.path),
        "workspace_timeline": samples,
    }
    if result.branch_path and result.branch_path.is_dir():
        record["passed_in_kept_branch"] = grade(task, result.branch_path)[0]
    if prepared.canary and prepared.canary.exists():
        prepared.canary.unlink()
    record["crashed"] = crashed
    (out / f"{arm_name}__{task.id}.json").write_text(json.dumps(record, indent=2, default=str))
    flag = "PASS" if passed else ("pass-in-branch" if record.get("passed_in_kept_branch") else "FAIL")
    print(f" {flag}  {record['wall_seconds']}s  ${record.get('cost_usd') or 0:.3f}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default="raw_claude,raw_codex,new_claude,new_codex")
    parser.add_argument("--tasks", default="", help="comma-separated task ids, e.g. 1_easy,3_hard")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    tasks = load_tasks(set(filter(None, args.tasks.split(","))) or None)
    arms = build_arms(args)
    chosen = [name for name in args.arms.split(",") if name]
    out = args.out or RESULTS / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="north-compare-"))
    print(f"results -> {out}\nworkspaces -> {work}")
    records = []
    try:
        for task in tasks:
            print(f"{task.id} ({task.level})")
            for name in chosen:
                records.append(run_one(name, arms[name], task, out, work))
    finally:
        (out / "all.json").write_text(json.dumps(records, indent=2, default=str))
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
