"""Browser automation, end to end, in your own Chrome profile: does it still work, and as fast as it did?

Opt in with NORTH_LIVE_BROWSER=1 and NORTH_LIVE_BROWSER_PROFILE=<a saved existing profile's id>; the
nightly check sets both when a profile is connected (orchestrator/nightly_check.py). It runs two
read-only Wikipedia tasks through experiments/browser_speed/run.py: a scratch north from this checkout,
your profile's TaskWindow bridge, and cards answered by the harness (yes only on the task's own site).

Two bugs lived for weeks because only a live run could show them: Enter never submitted a form, and
Chrome ran an extension copy five days old. Each task must finish, give the right facts, and stay
within its limits: about 1.5 times the slowest of the 2026-11-22 runs (wiki at most 4 browser calls,
list_profiles counted, and 21 s; chain 13 calls and 57 s), so a model's off night is not an alarm.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

_PROFILE = os.environ.get("NORTH_LIVE_BROWSER_PROFILE", "")
pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_BROWSER") != "1" or not _PROFILE,
    reason="explicit browser opt-in and a saved existing profile required",
)

_HARNESS = Path(__file__).parents[2] / "experiments" / "browser_speed"
# Facts each answer must hold, and the most browser calls and seconds a run may take.
_TASKS = {
    "wiki": (("1815",), 6, 45),
    "chain": (("Babbage", "Analytical"), 20, 120),
}


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"browser_speed_{name}", _HARNESS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, tuple[dict, dict]]:
    """Each task's result and numbers (browser calls, seconds), from one run of both."""
    run, compare = _module("run"), _module("compare")
    home = Path(os.environ.get("NORTH_HOME") or Path.home() / ".north")
    results, out = run.run_tasks(_PROFILE, list(_TASKS), results=home / "smoke" / "browser")
    db = sqlite3.connect(out / "tasks" / "tasks.db")
    return {r["task"]: (r, compare._task_numbers(db, r["task_id"], r["seconds"])) for r in results}


@pytest.mark.timeout(600)
@pytest.mark.parametrize("task", list(_TASKS))
def test_a_browser_task_finishes_with_the_right_facts_within_its_calls_and_time(runs, task) -> None:
    facts, most_calls, most_seconds = _TASKS[task]
    result, numbers = runs[task]

    assert result["status"] == "completed", f"{task} ended {result['status']}"
    missing = [fact for fact in facts if fact.lower() not in str(result["answer"]).lower()]
    assert not missing, f"{task}'s answer lacks {missing}: {str(result['answer'])[:300]}"
    assert numbers["browser_calls"] <= most_calls, (
        f"{task} took {numbers['browser_calls']:.0f} browser calls, more than {most_calls}"
    )
    assert numbers["wall_s"] <= most_seconds, f"{task} took {numbers['wall_s']:.0f} s, more than {most_seconds} s"
