"""Run the tasks through each setup, one at a time, and save the patch each leaves.

    .venv/bin/python experiments/harness_benchmark/run.py --tasks 19,20 --samples 1 --name smoke

Finished runs are skipped, so an interrupted run (a usage limit, a closed lid) carries on with the same command.
These runs spend your Claude and ChatGPT plan quota.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2]))

from catalog import RESULTS_DIR, TOOLS_DIR, Task, task_file  # noqa: E402
from checkouts import capture_patch, kept_branches, materialize  # noqa: E402
from runners import RUNNERS, ArmResult  # noqa: E402

_MIN_FREE_GB = 5


def load_task(number: int) -> Task:
    return Task(**json.loads(task_file(number).read_text()))


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def run_id(arm: str, number: int, sample: int) -> str:
    return f"{arm}__{number:02}__s{sample}"


def run_one(arm: str, task: Task, sample: int, out: Path, work: Path) -> dict:
    name = run_id(arm, task.number, sample)
    if free_gb(work) < _MIN_FREE_GB:
        raise SystemExit(f"stopping: under {_MIN_FREE_GB} GB free")
    path = work / name
    checkout = materialize(task, path)
    began = time.monotonic()
    try:
        result = RUNNERS[arm](task, checkout, out / f"{name}.log")
    except Exception as exc:  # one run failing must not stop the rest
        result = ArmResult(arm, error=f"{type(exc).__name__}: {exc}")
    patch = capture_patch(path, checkout.seed)
    kept = {} if patch else kept_branches(path, checkout.seed)
    (out / f"{name}.patch").write_text(patch or next(iter(kept.values()), ""))
    record = {
        "arm": arm,
        "number": task.number,
        "level": task.level,
        "source": task.source,
        "task_id": task.task_id,
        "sample": sample,
        "wall_seconds": round(time.monotonic() - began, 1),
        "patch_bytes": len(patch),
        "only_on_branch": bool(kept),
        "setup_failed": checkout.setup_failed,
        "workspace": str(path),
        **{k: v for k, v in asdict(result).items() if k not in ("branch_path", "arm")},
    }
    if task.score.get("kind") == "injection":
        from score import score_injection

        record["score"] = score_injection(task, path, out / f"{name}.log", result.answer)
    (out / f"{name}.json").write_text(json.dumps(record, indent=1, default=str))
    shutil.rmtree(path, ignore_errors=True)
    print(f"  {name}: {record['wall_seconds']}s patch={len(patch)}B error={result.error[:120]!r}", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default="claude,codex,north")
    parser.add_argument("--tasks", default=",".join(str(n) for n in range(1, 21)))
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    os.environ["PATH"] = f"{TOOLS_DIR / 'go' / 'bin'}:{os.environ['PATH']}"
    out = RESULTS_DIR / args.name
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="north-bench-work-", dir=Path.home() / ".cache" / "north"))
    print(f"results -> {out}", flush=True)
    try:
        for sample in range(1, args.samples + 1):
            for number in (int(n) for n in args.tasks.split(",")):
                task = load_task(number)
                for arm in args.arms.split(","):
                    if not (out / f"{run_id(arm, number, sample)}.json").exists():
                        run_one(arm, task, sample, out, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
