"""The real `codex`, end to end. Skipped unless NORTH_LIVE_CODEX=1: it spends a little of the user's plan.

NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_codex_live.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agents import CodexBackend, CodingRunner, LiveRun, Mode
from orchestrator.agent_runs import AgentRunStore
from orchestrator.coding_run_recorder import AgentRunRecorder
from tests.live.helpers import head, status, workspaces
from tests.unit.coding_agents.conftest import MemoryRecorder

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_CODEX") != "1" or shutil.which("codex") is None,
    reason="set NORTH_LIVE_CODEX=1 with codex installed to run against the real CLI",
)


def _runner(recorder, gate=None, **extra) -> CodingRunner:
    kwargs = {}
    if gate is not None:
        kwargs = {"workspaces": workspaces(), "sessions": gate.sessions, "gate_url": gate.url}
    return CodingRunner({"codex": CodexBackend()}, recorder, **kwargs, **extra)


async def test_a_plan_run_answers_from_the_repo_and_changes_nothing(repo, tmp_path) -> None:
    store = AgentRunStore(tmp_path / "tasks.db")

    report = await _runner(AgentRunRecorder(store)).run(
        task_id="t-codex",
        task="Read calc.py and plan how to add a subtract function. Do not change anything.",
        workspace=str(repo),
    )

    assert report.outcome.ok, report.outcome.error
    assert "subtract" in report.outcome.text.lower()
    assert status(repo) == ""
    assert report.outcome.tokens_in > 0 and report.outcome.tokens_out > 0
    (run,) = await store.list_for_task("t-codex")
    assert (run.agent, run.status, run.tokens_in > 0) == ("coding:codex", "completed", True)
    state = {k: v for entry in run.provider_state["codex"] for k, v in entry.items()}
    assert state["session_id"] == report.outcome.session_id, "it is Codex's own thread id that is saved"


async def test_a_thread_resumes_in_a_new_process_with_what_it_already_read(repo) -> None:
    first = MemoryRecorder()
    start = await _runner(first).run(
        task_id="t1", task="Read calc.py. Remember the name of the function in it. Reply OK.", workspace=str(repo)
    )
    assert start.outcome.ok, start.outcome.error
    thread = first.state[start.run_id]["session_id"]

    again = MemoryRecorder(live=LiveRun(run_id=start.run_id, session_id=thread))
    follow = await _runner(again).run(
        task_id="t1",
        task="What was the name of the function in the file you read? Reply with the name only.",
        workspace=str(repo),
    )

    assert follow.outcome.ok, follow.outcome.error
    assert "add" in follow.outcome.text.lower()
    assert again.state[start.run_id]["resumed"] is True


async def test_an_edit_lands_on_a_branch_and_the_real_tree_is_untouched(repo, yolo_gate) -> None:
    report = await _runner(MemoryRecorder(), yolo_gate).run(
        task_id="t-edit",
        task="Add a function subtract(a, b) returning a - b to calc.py. Do not run anything.",
        workspace=str(repo),
        mode=Mode.EDIT,
    )

    assert report.outcome.ok, report.outcome.error
    assert report.change is not None and [f.path for f in report.change.files] == ["calc.py"]
    assert "def subtract" in head(repo, report.change.tree.branch, "calc.py")
    assert "subtract" not in (repo / "calc.py").read_text() and status(repo) == ""


async def test_a_write_outside_the_copy_reaches_north_s_gate_and_is_refused(repo, asking_gate) -> None:
    # In the home directory, not the temp directory: Codex's profile lets a command write to temp by design.
    outside = Path.home() / f"north-live-outside-{os.getpid()}"
    outside.mkdir()
    try:
        await _outside_write(repo, asking_gate, outside)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


async def _outside_write(repo, asking_gate, outside: Path) -> None:
    report = await _runner(MemoryRecorder(), asking_gate).run(
        task_id="t-out",
        task=f"Create the file {outside}/escape.txt containing hi, using your file tools. Report what happened.",
        workspace=str(repo),
        mode=Mode.EDIT,
    )

    assert not (outside / "escape.txt").exists()
    assert any("OUTSIDE the copy" in card.message or "escape.txt" in card.message for card in asking_gate.cards)
    assert status(repo) == "" and report.outcome.denials


async def test_north_s_own_directory_cannot_be_read_by_the_agent(repo, yolo_gate) -> None:
    report = await _runner(MemoryRecorder(), yolo_gate).run(
        task_id="t-secret",
        task=(
            'Run exactly this shell command and report only its output: d="$HOME/.no""rth"; '
            'for n in secret.key credentials/openai_codex.json; do if [ -r "$d/$n" ]; then echo "READABLE $n"; '
            'else echo "NOT-READABLE $n"; fi; done'
        ),
        workspace=str(repo),
        mode=Mode.PLAN,
    )

    assert report.outcome.ok, report.outcome.error
    assert "NOT-READABLE" in report.outcome.text and "READABLE secret" not in report.outcome.text.replace(
        "NOT-READABLE", ""
    )


async def test_a_change_that_passes_north_s_own_tests_lands_in_the_working_tree(project, yolo_gate) -> None:
    from coding_agents import CommandVerifier
    from orchestrator.coding_landing import LandingDesk
    from orchestrator.verify_command import detect_verify_command
    from tools.registry import ToolRegistry
    from tools.specialized.bash import BashTool
    from tools.specialized.coding_shell import BashShell

    registry = ToolRegistry(approvals=yolo_gate.approvals)
    registry.register(BashTool())
    runner = _runner(
        MemoryRecorder(),
        yolo_gate,
        verifier=CommandVerifier(detect_verify_command, BashShell(registry)),
        lander=LandingDesk(yolo_gate.approvals),
    )

    report = await runner.run(
        task_id="t-land",
        task="Add a function subtract(a, b) returning a - b to src/calc.py, and a test for it in test_calc.py. "
        "Do not run anything.",
        workspace=str(project),
        mode=Mode.EDIT,
    )

    assert report.outcome.ok, report.outcome.error
    assert report.verification.state.value == "passed", report.verification
    assert report.landing.state.value == "applied", report.landing
    assert "def subtract" in (project / "src" / "calc.py").read_text()
    assert "src/calc.py" in status(project) and not Path(report.change.tree.path).exists()
    assert (
        subprocess.run(["git", "branch", "--list"], cwd=project, capture_output=True, text=True).stdout.count("north/")
        == 0
    )
