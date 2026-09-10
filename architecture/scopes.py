"""Task-scoped authorization for edits to manifest-owned repository paths."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from architecture.contracts import ModuleManifest, ModuleManifestError, load_module_manifest


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


@dataclass(frozen=True)
class ScopeGuard:
    """A :class:`TaskEditScope` bound to a workspace and the module manifest.

    This is the object minted at the composition root and threaded, as an opaque
    :class:`utils.edit_scope.EditAuthorizer`, through the request, the agent
    payload, and finally :class:`tools.models.ToolInput`. It carries everything a
    mutation guard needs so that the tool - which must not import ``architecture``
    or know about manifests - can decide a single absolute path with one call.

    Scope is *server-owned*: the guard is constructed from a trusted
    :class:`TaskEditScope`, never from a tool's model-supplied parameters.
    """

    scope: TaskEditScope
    workspace: Path
    manifest: ModuleManifest

    def authorize(self, path: Path) -> str | None:
        """Return ``None`` when *path* may be edited, else a refusal reason."""
        return self.scope.authorize(path, self.workspace, self.manifest)


def build_guard(
    scope: TaskEditScope,
    workspace: Path | str,
    manifest: ModuleManifest | None = None,
) -> ScopeGuard:
    """Bind a scope to a workspace and manifest, loading the shipped manifest by default.

    Call this at the composition root (or wherever a task's permissions are
    decided) to produce an :class:`EditAuthorizer` the runtime can carry without
    depending on the ``architecture`` package.
    """
    return ScopeGuard(
        scope=scope,
        workspace=Path(workspace),
        manifest=manifest if manifest is not None else load_module_manifest(),
    )
