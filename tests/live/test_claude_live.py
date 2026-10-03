"""The real `claude`, end to end. Skipped unless NORTH_LIVE_CLAUDE=1: it spends a little of the user's plan.

Proves what the fake cannot: that the flags north passes are accepted by the installed Claude Code,
that a plan run leaves the repository untouched, and that a session really resumes.

    NORTH_LIVE_CLAUDE=1 .venv/bin/python -m pytest tests/live -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agents import ClaudeBackend, CodingRunner, LiveRun
from orchestrator.agent_runs import AgentRunStore
from orchestrator.coding_run_recorder import AgentRunRecorder
from tests.conftest import approving_store, bind_approvals
from tests.unit.coding_agents.conftest import MemoryRecorder
from tools.models import ToolInput
from tools.specialized.coding_agent import CodingAgentTool

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="set NORTH_LIVE_CLAUDE=1 with claude installed to run against the real CLI",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    (directory / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (directory / "CLAUDE.md").write_text("Use type hints.\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=directory, check=True)
    subprocess.run([*git, "add", "."], cwd=directory, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=directory, check=True)
    return directory


def _status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout


async def test_a_plan_run_answers_from_the_repo_and_changes_nothing(repo, tmp_path) -> None:
    store = AgentRunStore(tmp_path / "tasks.db")
    runner = CodingRunner(
        {"claude": ClaudeBackend(protected_paths=[str(tmp_path / "north_home")])}, AgentRunRecorder(store)
    )
    tool = bind_approvals(CodingAgentTool(runner), store=approving_store())

    result = await tool.execute(
        ToolInput(
            params={
                "task_id": "t-live",
                "task": "Read calc.py and plan how to add a subtract function. Do not edit anything.",
            },
            granted_workspace=str(repo),
        )
    )

    assert result.success, result.error
    assert "subtract" in result.data["answer"].lower()
    assert _status(repo) == "", "a plan run must leave the repository exactly as it found it"
    (run,) = await store.list_for_task("t-live")
    assert (run.agent, run.status) == ("coding:claude", "completed")
    state = {key: value for entry in run.provider_state["claude_code"] for key, value in entry.items()}
    assert state["session_id"] and state["pid"] and state["cli_version"]
    assert run.cost_usd >= 0


async def test_a_session_resumes_with_what_it_already_read(repo) -> None:
    first = MemoryRecorder()
    runner = CodingRunner({"claude": ClaudeBackend()}, first)
    start = await runner.run(
        task_id="t1", task="Read calc.py. Remember the name of the function in it. Reply OK.", workspace=str(repo)
    )
    assert start.outcome.ok, start.outcome.error
    session = first.state[start.run_id]["session_id"]

    again = MemoryRecorder(live=LiveRun(run_id=start.run_id, session_id=session))
    follow = await CodingRunner({"claude": ClaudeBackend()}, again).run(
        task_id="t1",
        task="What was the name of the function in the file you read? Reply with the name only.",
        workspace=str(repo),
    )

    assert follow.outcome.ok, follow.outcome.error
    assert "add" in follow.outcome.text.lower()
    assert again.state[start.run_id]["resumed"] is True
    assert _status(repo) == ""


# ── Edit mode: the real hook, the real daemon route, the real approval layer ──────────────────────────


def _head(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True).stdout


async def _edit(repo: Path, gate, task: str, *, gate_url: str | None = None, recorder=None):
    from coding_agents import Mode
    from orchestrator.coding_workspaces import GitWorkspaces

    runner = CodingRunner(
        {"claude": ClaudeBackend()},
        recorder or MemoryRecorder(),
        workspaces=GitWorkspaces(),
        sessions=gate.sessions,
        gate_url=gate_url or gate.url,
    )
    return await runner.run(task_id="t-edit", task=task, workspace=str(repo), mode=Mode.EDIT)


@pytest.fixture
async def yolo_gate():
    from approval.models import ApprovalDecision  # noqa: F401
    from config.approval_mode import ApprovalMode
    from tests.live.support import start_gate

    gate = await start_gate(ApprovalMode.YOLO)
    yield gate
    await gate.stop()


@pytest.fixture
async def asking_gate():
    """Ask mode, and every card that appears is rejected."""
    from approval.models import ApprovalDecision
    from config.approval_mode import ApprovalMode
    from tests.live.support import start_gate

    gate = await start_gate(ApprovalMode.ASK, answer=ApprovalDecision.REJECTED)
    yield gate
    await gate.stop()


async def test_an_edit_lands_on_a_branch_and_the_real_tree_is_untouched(repo, yolo_gate) -> None:
    report = await _edit(
        repo,
        yolo_gate,
        "Add a function subtract(a, b) returning a - b to calc.py, using the Edit tool. Do not run anything.",
    )

    assert report.outcome.ok, report.outcome.error
    assert report.change is not None and [f.path for f in report.change.files] == ["calc.py"]
    assert "def subtract" in _head(repo, report.change.tree.branch, "calc.py")
    assert "subtract" not in (repo / "calc.py").read_text(), "the real working tree is untouched"
    assert _status(repo) == ""
    assert any(r.tool == "Edit" for r in yolo_gate.judge.asked) or any(r.tool == "Write" for r in yolo_gate.judge.asked)
    assert yolo_gate.sessions.lookup("anything") is None


async def test_a_declined_command_is_denied_but_edits_in_the_copy_never_needed_a_card(repo, asking_gate) -> None:
    report = await _edit(
        repo,
        asking_gate,
        "Do two things. 1) Add a function subtract(a, b) returning a - b to calc.py using the Edit tool. "
        "2) Run `python3 -c 'print(1)'` with the Bash tool and report what it said.",
    )

    assert report.change is not None and "def subtract" in _head(repo, report.change.tree.branch, "calc.py")
    assert any(r.tool == "Bash" for r in asking_gate.judge.asked), "the command went to the gate"
    assert any("python3" in card.message for card in asking_gate.cards), "and a card asked about it"
    assert not any("calc.py" in card.message for card in asking_gate.cards), "an edit inside the copy asked nobody"


async def test_a_write_outside_the_copy_is_refused_however_the_path_is_spelled(repo, asking_gate, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plain.txt").write_text("keep")
    task = (
        f"Use the Write tool to create these files and report each tool result exactly: "
        f"1) {outside}/absolute.txt  2) ../escape-relative.txt  3) calc.py/../../escape-traversal.txt  "
        f"4) {outside}/plain.txt with content overwritten. Call the tools without checking anything first."
    )

    report = await _edit(repo, asking_gate, task)

    assert not (outside / "absolute.txt").exists()
    assert (outside / "plain.txt").read_text() == "keep"
    assert not list(tmp_path.glob("**/escape-*.txt")) and not list(repo.parent.glob("escape-*.txt"))
    assert any(r.tool == "Write" for r in asking_gate.judge.asked)
    assert any("OUTSIDE the copy" in card.message for card in asking_gate.cards)
    assert _status(repo) == "" and report.outcome.error is not None


async def test_git_s_pointer_file_is_never_overwritten_even_when_everything_else_is_allowed(repo, yolo_gate) -> None:
    report = await _edit(
        repo,
        yolo_gate,
        "First use the Read tool on the file named .git in the current directory. Then use the Write tool to "
        "overwrite that same file .git with exactly the text 'gitdir: /tmp/evil'. Report each tool result exactly.",
    )

    assert report.change is None
    assert any(d.tool == "Write" and d.detail.endswith(".git") for d in report.outcome.denials)
    assert not any(q.tool == "Write" for q in yolo_gate.judge.asked), "north's own rule refused it, nobody was asked"


async def test_with_the_gate_unreachable_nothing_changes(repo, yolo_gate) -> None:
    report = await _edit(
        repo,
        yolo_gate,
        "Use the Write tool to create a file named created.txt containing 'hi', then run `touch also-created` "
        "with the Bash tool. Report both tool results exactly.",
        gate_url="http://127.0.0.1:9/orchestrator/coding/gate",
    )

    assert report.change is None, "a broken gate means every change is blocked"
    assert report.outcome.denials, "and the agent's attempts are listed as refused"
    assert _status(repo) == ""
