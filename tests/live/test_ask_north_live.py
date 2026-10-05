"""A real Claude Code and a real Codex asking north a question, through the real MCP route and approval layer.

The question is one only the user could answer (a naming rule that is nowhere in the repository), so an agent
that does not ask cannot get it right. Skipped unless the CLI is installed and opted in.

NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_ask_north_live.py -q
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from approval.models import ApprovalDecision
from coding_agents import ClaudeBackend, CodexBackend, CodingRunner, Mode
from config.approval_mode import ApprovalMode
from tests.conftest import deciding
from tests.live.helpers import status, workspaces
from tests.live.support import start_gate
from tests.unit.coding_agents.conftest import MemoryRecorder
from utils.prompts import load_prompt

RULE = "nx_"
TASK = (
    "Plan adding a function that subtracts two numbers to calc.py. The user has a naming rule for new functions "
    "that is not written anywhere in this repository. Find out what it is, then state the exact function name."
)


def _enabled(flag: str, command: str) -> bool:
    return os.environ.get(flag) == "1" and shutil.which(command) is not None


BACKENDS = [
    pytest.param(
        "claude", marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CLAUDE", "claude"), reason="NORTH_LIVE_CLAUDE=1")
    ),
    pytest.param(
        "codex", marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CODEX", "codex"), reason="NORTH_LIVE_CODEX=1")
    ),
]


def _guidance(mode: Mode) -> str:
    """What the coding tool hands every run, so the agent knows it may ask."""
    prompt = load_prompt(f"prompts/coding_agent_{mode.value}.md")
    return prompt.format(repo_instructions="(none)", north_context="(nothing relevant)")


def _runner(backend: str, gate, tmp_path) -> CodingRunner:
    backends = {
        "claude": ClaudeBackend(protected_paths=[str(tmp_path / "north_home")]),
        "codex": CodexBackend(),
    }
    return CodingRunner(
        {backend: backends[backend]},
        MemoryRecorder(),
        workspaces=workspaces(),
        sessions=gate.sessions,
        gate_url=gate.url,
        ask_url=gate.ask_url,
    )


async def _user_answers(gate, text: str) -> None:
    """The person at the dashboard: answer the first question card that appears."""
    while True:
        for card in gate.store.pending():
            if card.type.value == "question":
                gate.cards.append(card)
                gate.store.resolve(card.id, ApprovalDecision.ANSWERED, chosen_option=text, decided_by="you")
                return
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_in_autonomous_mode_north_answers_the_agent_from_memory(repo, tmp_path, backend) -> None:
    decider = deciding(
        ApprovalDecision.ANSWERED, f"Every new function must be named with the prefix {RULE}", "your naming rule"
    )
    gate = await start_gate(ApprovalMode.AUTONOMOUS, decider=decider)
    try:
        report = await _runner(backend, gate, tmp_path).run(
            task_id="t-ask",
            task=TASK,
            workspace=str(repo),
            mode=Mode.PLAN,
            backend=backend,
            guidance=_guidance(Mode.PLAN),
        )
    finally:
        await gate.stop()

    assert report.outcome.ok, report.outcome.error
    assert f"{RULE}subtract" in report.outcome.text, report.outcome.text
    assert [e for e, _ in gate.ask_log.events] == ["ask"]
    assert gate.ask_log.events[0][1]["by"] == "memory_decider"
    assert gate.store.pending() == [] and status(repo) == ""


@pytest.mark.parametrize("backend", BACKENDS)
async def test_in_safe_mode_the_question_reaches_the_user_and_their_answer_reaches_the_agent(
    repo, tmp_path, backend
) -> None:
    gate = await start_gate(ApprovalMode.SAFE)
    try:
        answering = asyncio.create_task(_user_answers(gate, f"Use the prefix {RULE} on every new function"))
        report = await asyncio.wait_for(
            _runner(backend, gate, tmp_path).run(
                task_id="t-ask-safe",
                task=TASK,
                workspace=str(repo),
                mode=Mode.PLAN,
                backend=backend,
                guidance=_guidance(Mode.PLAN),
            ),
            timeout=240,
        )
        await asyncio.wait_for(answering, timeout=5)
    finally:
        await gate.stop()

    assert report.outcome.ok, report.outcome.error
    assert f"{RULE}subtract" in report.outcome.text, report.outcome.text
    [card] = gate.cards
    assert card.agent == "coding_agent" and card.task_id == "t-ask-safe" and "naming rule" in card.message.lower()
    assert gate.ask_log.events[0][1]["by"] == "you"


@pytest.mark.parametrize("backend", BACKENDS)
async def test_an_edit_run_can_ask_too_and_is_still_gated(repo, tmp_path, backend) -> None:
    decider = deciding(ApprovalDecision.ANSWERED, f"Prefix every new function with {RULE}", "your naming rule")
    gate = await start_gate(ApprovalMode.AUTONOMOUS, decider=decider)
    try:
        report = await _runner(backend, gate, tmp_path).run(
            task_id="t-ask-edit",
            task=(
                "Add a function that subtracts two numbers to calc.py. The user has a naming rule for new "
                "functions that is not written anywhere in this repository: ask north what it is, then follow it."
            ),
            workspace=str(repo),
            mode=Mode.EDIT,
            backend=backend,
            guidance=_guidance(Mode.EDIT),
        )
    finally:
        await gate.stop()

    assert report.outcome.ok, report.outcome.error
    assert report.change is not None, report.outcome.text
    from pathlib import Path

    assert f"def {RULE}subtract" in Path(report.change.tree.path, "calc.py").read_text()
    assert status(repo) == "", "the user's repository was not touched"
    assert [e for e, _ in gate.ask_log.events].count("ask") == 1
