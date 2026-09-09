"""Task-scoped authorization for edits to manifest-owned repository paths."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from architecture.contracts import ModuleManifest, ModuleManifestError


@dataclass(frozen=True)
class TaskEditScope:
    """Server-owned permissions for one task's source mutations.

    ``modules`` permits edits to ordinary paths owned by those modules.
    ``paths`` permits narrowly named paths regardless of module. A protected
    path always requires an explicit match in ``authorized_paths`` as well.
    """

    modules: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    authorized_paths: tuple[str, ...] = ()

    def authorize(self, path: Path, workspace: Path, manifest: ModuleManifest) -> str | None:
        """Return ``None`` when an edit is allowed, otherwise a refusal reason."""
        try:
            relative = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return "Path is outside the task workspace."

        try:
            contract = manifest.module_for(relative)
        except ModuleManifestError:
            return f"Path `{relative}` is not owned by a declared module."

        path_allowed = _matches(relative, self.paths)
        if contract.protected and not _matches(relative, self.authorized_paths):
            return f"Path `{relative}` is protected and requires explicit user authorization."
        if path_allowed or contract.name in self.modules:
            return None
        return f"Path `{relative}` is outside this task's permitted modules and paths."


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)
