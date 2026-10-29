"""The CLI shows the answer combining several agents, not the agents' answers run together."""

from __future__ import annotations

from unittest.mock import MagicMock

from cli._task_stream import TaskStepFeed
from cli.formatting import _reconstruct_task_output


def test_the_combined_answer_replaces_what_the_agents_streamed() -> None:
    feed = TaskStepFeed(stream=MagicMock(), live=MagicMock())
    feed.apply("token", {"text": "found three sources"})
    feed.apply("agent_completed", {"agent": "researcher"})
    feed.apply("token", {"text": "drafted the summary"})
    feed.apply("agent_completed", {"agent": "writer"})

    feed.apply("task_synthesis", {"output": "Three sources, summarised.", "agents": ["researcher", "writer"]})

    assert feed.answer == "Three sources, summarised."


def test_a_ledger_with_a_combined_answer_reads_as_that_answer() -> None:
    entries = [
        {"action": "agent_completed", "output": "found three sources"},
        {"action": "agent_completed", "output": "drafted the summary"},
        {"action": "task_synthesis", "output": "Three sources, summarised."},
    ]
    assert _reconstruct_task_output(entries) == "Three sources, summarised."
    assert _reconstruct_task_output(entries[:2]) == "found three sources\n\ndrafted the summary"


def test_an_event_nothing_emits_adds_no_step() -> None:
    """The feed rendered a failover line, but no code on the server has ever emitted that event."""
    feed = TaskStepFeed(stream=MagicMock(), live=MagicMock())
    feed.apply("failover", {"from": "a", "to": "b"})
    assert feed.steps == []
