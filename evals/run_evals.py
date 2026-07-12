"""Run the north coding scoreboard against a running north server.

Usage:
    # 1. Start north (autonomous approval mode so mutating tools don't block):
    NORTH_APPROVAL_MODE=autonomous .venv/bin/python -m uvicorn orchestrator.app:app --port 8000
    # 2. Run the scoreboard:
    .venv/bin/python -m evals.run_evals
    # optional: --only fix_average_empty  --min-pass-rate 0.8  --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from evals.harness import (
    HttpNorthClient,
    format_report,
    load_secret,
    load_tasks,
    run_all,
    summarize,
)

_TASKS_ROOT = Path(__file__).parent / "tasks"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run north's coding scoreboard.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="north server URL")
    parser.add_argument("--tasks", type=Path, default=_TASKS_ROOT, help="tasks directory")
    parser.add_argument("--only", default="", help="comma-separated task ids to run (default: all)")
    parser.add_argument("--work-dir", type=Path, default=None, help="where to build task workspaces")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=0.0,
        help="exit non-zero if the pass rate over graded tasks is below this (0-1)",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks)
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t.id in wanted]
    if not tasks:
        print(f"No tasks found under {args.tasks}", file=sys.stderr)
        return 2

    try:
        secret = load_secret()
    except OSError as exc:
        print(f"Could not read north secret: {exc}", file=sys.stderr)
        return 2

    client = HttpNorthClient(args.base_url, secret)
    work_root = args.work_dir or Path(tempfile.mkdtemp(prefix="north-evals-"))
    print(f"Running {len(tasks)} task(s) against {args.base_url} (workspaces in {work_root})")

    results = run_all(tasks, client, work_root)
    print(format_report(results))

    rate = summarize(results)["pass_rate"]
    if rate is not None and rate < args.min_pass_rate:
        print(f"\nPass rate {rate * 100:.0f}% is below the required {args.min_pass_rate * 100:.0f}%.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
