"""Every agent name north advertises to a model must resolve.

north used to tell the model, in the `delegate_task` schema and in the planner's
routing table, that it could hand work to `finance`, `health`, `university` and
`job`. None of them had an agent. A model that did exactly what it was told
raised `AgentNotFoundError`, so north caused the failure with its own
instructions - and it read like a model mistake, which is what made it hard to
attribute.

Fixing the four names is a one-line edit. These tests are the part that keeps
them fixed: an empty `agents/tester/` appeared after the bug was filed, so
scaffolding a directory and never finishing it is a thing that happens here.

No network, no LLM - these read the shipped artifacts only.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
PLANNER_PROMPT = REPO_ROOT / "prompts" / "planner.md"


def _registered_agent_names() -> set[str]:
    """Agent names `AgentRegistry` would actually register.

    Mirrors `AgentRegistry._is_valid_agent_directory` rather than importing the
    registry, which needs constructed dependencies. The rule under test is about
    what is on disk, so reading disk is the honest check.
    """
    return {
        entry.name
        for entry in AGENTS_DIR.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and (entry / "config.yaml").exists()
        and (entry / "agent.py").exists()
    }


def _agent_domains() -> set[str]:
    """Every domain declared by a registered agent."""
    domains = set()
    for name in _registered_agent_names():
        config = yaml.safe_load((AGENTS_DIR / name / "config.yaml").read_text(encoding="utf-8"))
        if isinstance(config, dict) and config.get("domain"):
            domains.add(str(config["domain"]))
    return domains


def test_delegate_task_advertises_only_registered_agents() -> None:
    """The schema names come from the registry, so they cannot be stale."""
    from agents.schemas import delegate_task_schema

    names = sorted(_registered_agent_names())
    description = delegate_task_schema(names)["function"]["parameters"]["properties"]["agent"]["description"]

    for name in names:
        assert f"'{name}'" in description, f"{name} is registered but not advertised"
    for dead in ("finance", "health", "university", "job", "tester"):
        assert f"'{dead}'" not in description, f"{dead} has no agent and must not be advertised"


def test_delegate_task_schema_names_nothing_without_a_registry() -> None:
    """With no registry the schema must not fall back to a guessed list.

    A guessed list is exactly what this bug was. Saying less is correct here.
    """
    from agents.schemas import delegate_task_schema

    description = delegate_task_schema()["function"]["parameters"]["properties"]["agent"]["description"]
    assert "registered in this install" in description
    assert "'" not in description, "no agent name should be named when none are known"


def test_no_scaffolded_agent_directories() -> None:
    """A directory under agents/ is either a real agent or not there.

    `AgentRegistry` skips an incomplete directory silently, which is how four of
    them survived long enough to be advertised.
    """
    incomplete = [
        entry.name
        for entry in AGENTS_DIR.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and entry.name != "__pycache__"
        and not ((entry / "config.yaml").exists() and (entry / "agent.py").exists())
    ]
    assert not incomplete, f"scaffolded but unregistered agent directories: {incomplete}"


def test_planner_routing_table_targets_resolve() -> None:
    """Every target in the planner's classification table must exist.

    The table routes by domain, except `news_briefing`, which is an agent name.
    Both are legitimate; a name that is neither is the bug.
    """
    rows = re.findall(r"^\|(?!\s*-)[^|]+\|\s*`([a-z_]+)`\s*\|", PLANNER_PROMPT.read_text(encoding="utf-8"), re.M)
    assert rows, "no routing rows parsed - the table's shape changed, update this test"

    resolvable = _registered_agent_names() | _agent_domains()
    unresolvable = sorted(set(rows) - resolvable)
    assert not unresolvable, f"planner routes to targets with no agent or domain: {unresolvable}"
