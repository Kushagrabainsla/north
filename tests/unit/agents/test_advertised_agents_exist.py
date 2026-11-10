"""North is the one agent, and nothing tells a model about any other.

north used to advertise agents that did not exist (`finance`, `health`,
`university`, `job`) in a delegation schema and a planner routing table; a model
that did what it was told raised `AgentNotFoundError`. Both are gone. These tests
keep what is on disk honest: one agent, and no half-scaffolded agent folders.

No network, no LLM - these read the shipped artifacts only.
"""

from __future__ import annotations

from pathlib import Path

from agents.capabilities import build_platform_capabilities_summary
from agents.schemas import ASK_USER_SCHEMA, FIND_TOOLS_SCHEMA, REQUEST_APPROVAL_SCHEMA

REPO_ROOT = Path(__file__).parent.parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"


def _registered_agent_names() -> set[str]:
    """Agent names `AgentRegistry` would actually register (its `_is_valid_agent_directory` rule)."""
    return {
        entry.name
        for entry in AGENTS_DIR.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and (entry / "config.yaml").exists()
        and (entry / "agent.py").exists()
    }


def test_no_scaffolded_agent_directories() -> None:
    """A directory under agents/ is either a real agent or not there."""
    incomplete = [
        entry.name
        for entry in AGENTS_DIR.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and entry.name != "__pycache__"
        and not ((entry / "config.yaml").exists() and (entry / "agent.py").exists())
    ]
    assert not incomplete, f"scaffolded but unregistered agent directories: {incomplete}"


def test_north_is_the_one_agent() -> None:
    """A specialism is a skill on north, never an agent of its own (CODING_STYLE §15)."""
    assert _registered_agent_names() == {"north"}


def test_no_tool_north_is_offered_hands_work_to_another_agent() -> None:
    names = {schema["function"]["name"] for schema in (ASK_USER_SCHEMA, FIND_TOOLS_SCHEMA, REQUEST_APPROVAL_SCHEMA)}

    assert "delegate_task" not in names


def test_north_is_not_told_it_has_specialist_agents() -> None:
    summary = build_platform_capabilities_summary(object())

    assert "create_agent" not in summary
    assert "Specialized Agents" not in summary
    assert "delegate" not in summary.lower()
