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
_STOP_AFTER_FAILURES = 3


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
    shutil.rmtree(path, ignore_errors=True)
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


def finished(record_file: Path) -> bool:
    """Whether a run is done. One that failed before doing any work (a usage limit, an outage) runs again."""
    if not record_file.exists():
        return False
    record = json.loads(record_file.read_text())
    return not (record.get("error") and not record.get("patch_bytes"))


def no_work(record: dict) -> bool:
    return bool(record.get("error")) and not record.get("patch_bytes")


def check_setup(numbers: list[int], work: Path) -> int:
    """Build each task's copy and install its dependencies, spending no model quota."""
    broken = 0
    for number in numbers:
        task = load_task(number)
        path = work / f"check-{number:02}"
        began = time.monotonic()
        try:
            copy = materialize(task, path)
            status = "ok" if not copy.setup_failed else f"setup failed: {copy.setup_failed}"
        except Exception as exc:
            status, broken = f"BROKEN {type(exc).__name__}: {str(exc)[:300]}", broken + 1
        print(f"{number:2} {time.monotonic() - began:6.0f}s {free_gb(work):5.1f} GB free  {status[:500]}", flush=True)
        shutil.rmtree(path, ignore_errors=True)
    return 1 if broken else 0


def run_all(args: argparse.Namespace, numbers: list[int], out: Path, work: Path) -> None:
    in_a_row = 0
    for sample in range(1, args.samples + 1):
        for number in numbers:
            task = load_task(number)
            for arm in args.arms.split(","):
                if finished(out / f"{run_id(arm, number, sample)}.json"):
                    continue
                in_a_row = in_a_row + 1 if no_work(run_one(arm, task, sample, out, work)) else 0
                if in_a_row >= _STOP_AFTER_FAILURES:
                    # Most likely a usage limit: carrying on would only record failures. Rerun to continue.
                    raise SystemExit(f"stopping: {in_a_row} runs in a row did no work")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default="claude,codex,north")
    parser.add_argument("--tasks", default=",".join(str(n) for n in range(1, 21)))
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--name", default="")
    parser.add_argument("--check-setup", action="store_true")
    args = parser.parse_args()

    os.environ["PATH"] = f"{TOOLS_DIR / 'go' / 'bin'}:{os.environ['PATH']}"
    numbers = [int(n) for n in args.tasks.split(",")]
    work = Path(tempfile.mkdtemp(prefix="north-bench-work-", dir=Path.home() / ".cache" / "north"))
    try:
        if args.check_setup:
            return check_setup(numbers, work)
        out = RESULTS_DIR / args.name
        out.mkdir(parents=True, exist_ok=True)
        print(f"results -> {out}", flush=True)
        run_all(args, numbers, out, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
