"""Project/workspace provenance keeps episodic retrieval relevant."""
from pathlib import Path

from memory.episodic import EpisodicStore


async def test_search_filters_episodes_to_current_project(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path / "episodic.db")
    await store.record(
        "north-task",
        "engineering",
        "North loads tools from tools/registry.py.",
        project_id="github.com/north",
        workspace_id="workspace:north",
    )
    await store.record(
        "acme-task",
        "engineering",
        "Acme loads tools from plugins/registry.py.",
        project_id="github.com/acme",
        workspace_id="workspace:acme",
    )

    results = await store.search(
        "where are tools loaded",
        allowed_domains=frozenset({"engineering"}),
        project_id="github.com/north",
        workspace_id="workspace:north",
    )

    assert results == ["North loads tools from tools/registry.py."]


async def test_search_prefers_current_workspace_within_project(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path / "episodic.db")
    await store.record(
        "old-task",
        "engineering",
        "The project uses the legacy worker configuration.",
        project_id="github.com/north",
        workspace_id="workspace:old",
    )
    await store.record(
        "current-task",
        "engineering",
        "The project uses the current orchestrator configuration.",
        project_id="github.com/north",
        workspace_id="workspace:current",
    )

    results = await store.search(
        "project configuration",
        allowed_domains=frozenset({"engineering"}),
        project_id="github.com/north",
        workspace_id="workspace:current",
        max_results=1,
    )

    assert results == ["The project uses the current orchestrator configuration."]
