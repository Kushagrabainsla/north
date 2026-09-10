from __future__ import annotations

from pathlib import Path

from architecture.contracts import load_module_manifest
from architecture.scopes import TaskEditScope

ROOT = Path(__file__).parents[3]


def test_scope_allows_path_owned_by_permitted_module() -> None:
    scope = TaskEditScope(modules=("platform.config",))

    assert scope.authorize(ROOT / "config/settings.py", ROOT, load_module_manifest()) is None


def test_scope_denies_cross_module_path() -> None:
    scope = TaskEditScope(modules=("platform.config",))

    reason = scope.authorize(ROOT / "agents/models.py", ROOT, load_module_manifest())

    assert reason == "Path `agents/models.py` is outside this task's permitted modules and paths."


def test_scope_requires_explicit_authorization_for_protected_path() -> None:
    scope = TaskEditScope(modules=("architecture",))
    authorized = TaskEditScope(modules=("architecture",), authorized_paths=("architecture/scopes.py",))

    assert "requires explicit user authorization" in (
        scope.authorize(ROOT / "architecture/scopes.py", ROOT, load_module_manifest()) or ""
    )
    assert authorized.authorize(ROOT / "architecture/scopes.py", ROOT, load_module_manifest()) is None
