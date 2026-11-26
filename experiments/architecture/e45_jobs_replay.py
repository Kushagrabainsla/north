"""#45: would removing the plain-prompt schedule path change any real job?

Usage: e45_jobs_replay.py

Reads north's job queue (`~/.north/jobs.db`, read-only) and sorts every scheduled or one-off job by the
path `main` sends it down: a flow, or a plain prompt run as a task. Then it runs the branch's `flow_of`
on each job, which raises for a job with no flow. The results hold counts and dates only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jobs.exceptions import JobNeedsAttention  # noqa: E402
from jobs.models import Job, JobType  # noqa: E402
from orchestrator.app import flow_of  # noqa: E402

DB = Path.home() / ".north" / "jobs.db"
RESULTS = Path(__file__).resolve().parent / "results"


def main() -> None:
    rows = sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute(
        "select job_id, type, agent, task, payload, created_epoch from job_queue"
    )
    paths: Counter[str] = Counter()
    last_prompt = 0.0
    since = Counter()
    raised = 0
    cutoff = datetime(2026, 9, 25, tzinfo=UTC).timestamp()
    for job_id, kind, agent, task, payload, created in rows:
        data = json.loads(payload or "{}")
        path = "flow" if str(data.get("flow") or "").strip() else "plain prompt"
        paths[path] += 1
        if path == "plain prompt":
            last_prompt = max(last_prompt, created)
        if created >= cutoff:
            since[path] += 1
            job = Job(
                job_id=job_id,
                type=JobType(kind),
                agent=agent,
                task=task,
                payload=data,
                scheduled_at=datetime.fromtimestamp(created, UTC),
            )
            try:
                flow_of(job)
            except JobNeedsAttention:
                raised += 1
    result = {
        "all_jobs_by_path": dict(paths),
        "last_plain_prompt_job": datetime.fromtimestamp(last_prompt, UTC).date().isoformat() if last_prompt else None,
        "jobs_since_2026_09_25_by_path": dict(since),
        "jobs_since_2026_09_25_flow_of_raised": raised,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "e45_jobs_replay.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
