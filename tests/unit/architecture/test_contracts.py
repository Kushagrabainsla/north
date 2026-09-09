from __future__ import annotations

from pathlib import Path

import pytest

from architecture.contracts import ModuleManifestError, load_module_manifest

ROOT = Path(__file__).parents[3]
PRODUCTION_ROOTS = (
    "agents",
    "approval",
    "bootstrap",
    "cli",
    "config",
    "context",
    "gateways",
    "inference",
    "jobs",
    "ledger",
    "mcp",
    "memory",
    "orchestrator",
    "skills",
    "tools",
    "utils",
    "web",
)


def test_every_tracked_source_path_has_one_module_owner() -> None:
    manifest = load_module_manifest()

    owners = {path: manifest.module_for(path).name for path in _production_paths()}

    assert len(owners) == len(_production_paths())
    assert owners["orchestrator/app.py"] == "composition.app"
    assert owners["config/dependencies.py"] == "composition.app"
    assert owners["tools/universal/update_plan.py"] == "integrations.tools"


def test_protected_architecture_and_safety_modules_are_not_agent_editable() -> None:
    manifest = load_module_manifest()

    assert manifest.modules["architecture"].protected
    assert manifest.modules["safety_policy"].protected
    assert manifest.modules["architecture"].editable_by == ()
    assert manifest.modules["safety_policy"].editable_by == ()


def test_module_for_rejects_unowned_paths() -> None:
    with pytest.raises(ModuleManifestError, match="exactly one module"):
        load_module_manifest().module_for("README.md")


def _production_paths() -> set[str]:
    paths: set[str] = {"exceptions.py", "policies/safety.md", "policies/clean-code.md", "SECURITY.md"}
    for directory in PRODUCTION_ROOTS:
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.endswith((".pyc", ".tsbuildinfo")) or relative in {
                "web/vite.config.js",
                "web/vite.config.d.ts",
            }:
                continue
            paths.add(relative)
    return paths
