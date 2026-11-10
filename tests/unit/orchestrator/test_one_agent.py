"""North is the one agent: a task goes straight to it, with no planner model call in front.

A planner model used to classify every task first - about 4 seconds (2 to 12)
before every answer - to pick the one agent there was. What it decided that still
matters is read from the ask's words: whether it acts outside north (the goal
check), and whether it is a plain overview of the chosen repository (the quick
read-only budget).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.models import AgentResult
from approval.approval_memory import ApprovalMemory
from approval.interaction import UserInteraction
from approval.store import ApprovalStore
from orchestrator.models import TaskRequest
from orchestrator.orchestrator import Orchestrator
from orchestrator.task_intent import execution_profile, is_consequential

# ── a task runs on north at once ─────────────────────────────────────────────


class _North:
    name = "north"
    domain = "general"

    def __init__(self) -> None:
        self.payloads = []
        self.deps = MagicMock(agent_run_store=None)

    async def run(self, payload):
        self.payloads.append(payload)
        return AgentResult(output="It is sunny.", summary="It is sunny.", data={})


def _orchestrator(agent: _North) -> Orchestrator:
    ledger = MagicMock()
    ledger.write = AsyncMock()
    stream = MagicMock()
    stream.emit = AsyncMock()
    stream.emit_done = AsyncMock()
    store = ApprovalStore()
    orchestrator = Orchestrator(
        ledger=ledger,
        agent=agent,
        north_star_checker=MagicMock(check_alignment=AsyncMock(return_value=(True, None, "fine"))),
        failure_handler=MagicMock(),
        interaction=UserInteraction(store, notifier=MagicMock(notify=AsyncMock()), stream_manager=stream),
        stream_manager=stream,
        approval_store=store,
    )
    orchestrator._auditor.audit = AsyncMock()
    return orchestrator


async def test_a_task_runs_on_north_with_no_planner_call() -> None:
    north = _North()
    orchestrator = _orchestrator(north)

    await orchestrator._run_pipeline("t1", TaskRequest(prompt="what is the weather today?"))

    assert [payload.prompt for payload in north.payloads] == ["what is the weather today?"]
    events = [call.args[1] for call in orchestrator._stream_manager.emit.call_args_list]
    assert not {"classifying", "classified", "routing", "routed"} & set(events)
    assert events[0] == "executing"
    orchestrator._north_star_checker.check_alignment.assert_not_awaited()  # a question acts on nothing
    actions = [call.args[0].action for call in orchestrator._ledger.write.call_args_list]
    assert "agent_completed" in actions and actions[-1] == "task_completed"
    assert not hasattr(orchestrator, "_execution_planner")


async def test_an_ask_that_acts_outside_north_is_goal_checked_before_north_runs() -> None:
    north = _North()
    orchestrator = _orchestrator(north)

    await orchestrator._run_pipeline("t1", TaskRequest(prompt="send the application email to Acme"))

    orchestrator._north_star_checker.check_alignment.assert_awaited_once()
    assert len(north.payloads) == 1


async def test_a_repository_overview_gets_the_quick_read_only_budget(tmp_path: Path) -> None:
    north = _North()
    orchestrator = _orchestrator(north)

    request = TaskRequest(prompt="Give me an overview of this repo", workspace=str(tmp_path))
    await orchestrator._run_pipeline("t1", request)

    assert north.payloads[0].execution_profile == "quick_readonly"
    assert north.payloads[0].workspace == str(tmp_path)


# ── is_consequential ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "send the email to my landlord",
        "apply to the Anthropic job",
        "submit the form for me",
        "buy two tickets for Friday",
        "delete the old backups",
        "pay the electricity bill",
        "create a flow that checks my mail",
        "set up a skill for weekly reviews",
        "add a tool for my thermostat",
        "schedule an automation for 8am",
    ],
)
def test_an_ask_that_acts_outside_north_is_consequential(prompt: str) -> None:
    assert is_consequential(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["what is on my calendar today?", "explain how this function works", "summarize this article", "open my LinkedIn"],
)
def test_a_question_or_a_read_is_not(prompt: str) -> None:
    assert not is_consequential(prompt)


def test_a_fact_about_you_does_not_flag_every_task_but_the_recent_conversation_does() -> None:
    facts = "## What north knows about you\n- You delete old photos every month.\n"
    assert not is_consequential("yes, go ahead", facts)

    dialogue = facts + "\n## Recent conversation\nUser: send my application email to Acme\nNorth: Shall I?\n"
    assert is_consequential("yes, go ahead", dialogue)


# ── execution_profile ────────────────────────────────────────────────────────


def test_a_plain_overview_of_the_chosen_repository_is_quick_and_read_only() -> None:
    assert execution_profile("Give me an overview of this repository.", "/repo") == "quick_readonly"
    assert execution_profile("How does this codebase work?", "/repo") == "quick_readonly"


def test_deep_work_or_no_repository_keeps_the_full_budget() -> None:
    assert execution_profile("Give me an overview of this repo and fix the failing test.", "/repo") == "standard"
    assert execution_profile("Audit this repository for security issues.", "/repo") == "standard"
    assert execution_profile("Give me an overview of this repository.", "") == "standard"
    assert execution_profile("what is the weather?", "/repo") == "standard"


# ── the one agent was called "general" ──────────────────────────────────────


def test_past_decisions_made_under_general_are_north_s_now(tmp_path: Path) -> None:
    """The memory decider is shown past decisions by agent, so they follow the rename."""
    path = tmp_path / "approval_memory.db"
    memory = ApprovalMemory(path)
    memory.record("general", "the research-paper-briefing candidate passed", "approved")
    memory.record("browser", "open example.com", "approved")

    ApprovalMemory(path)  # opening it again renames, once

    with sqlite3.connect(path) as conn:
        agents = sorted(row[0] for row in conn.execute("SELECT agent FROM decisions"))
    assert agents == ["browser", "north"]
