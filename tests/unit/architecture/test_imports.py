from __future__ import annotations

from pathlib import Path

from architecture.contracts import load_module_manifest
from architecture.imports import (
    ImportEdge,
    forbidden_import_edges,
    load_import_baseline,
    local_import_edges,
)

ROOT = Path(__file__).parents[3]


def test_current_forbidden_import_pairs_match_compatibility_baseline() -> None:
    manifest = load_module_manifest()

    assert forbidden_import_edges(ROOT, manifest) == load_import_baseline(manifest)


def test_local_import_edges_classify_owned_imports(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "config" / "settings.py").write_text("VALUE = 1\n")
    (tmp_path / "agents" / "runner.py").write_text("from config.settings import VALUE\n")

    edges = local_import_edges(tmp_path, load_module_manifest())

    assert edges == {ImportEdge("application.agents", "platform.config")}
    assert forbidden_import_edges(tmp_path, load_module_manifest()) == set()


def test_forbidden_import_edges_reject_outward_dependency(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "config" / "settings.py").write_text("from agents.runner import run\n")
    (tmp_path / "agents" / "runner.py").write_text("def run(): pass\n")

    assert forbidden_import_edges(tmp_path, load_module_manifest()) == {
        ImportEdge("platform.config", "application.agents")
    }
