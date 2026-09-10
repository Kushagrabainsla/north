"""Analyze local Python imports against North's module-boundary manifest."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from architecture.contracts import ModuleContract, ModuleManifest, ModuleManifestError


@dataclass(frozen=True, order=True)
class ImportEdge:
    """One direct local import between two owned architectural modules."""

    source: str
    target: str


def local_import_edges(root: Path, manifest: ModuleManifest) -> set[ImportEdge]:
    """Return direct cross-module imports among manifest-owned Python sources."""
    source_files = _owned_python_files(root, manifest)
    import_targets = _import_targets(source_files)
    edges: set[ImportEdge] = set()

    for path, source in source_files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = _package_for(path.relative_to(root))
        for imported in _imported_names(tree, package):
            target = _target_for(imported, import_targets)
            if target is not None and target.name != source.name:
                edges.add(ImportEdge(source.name, target.name))
    return edges


def forbidden_import_edges(root: Path, manifest: ModuleManifest) -> set[ImportEdge]:
    """Return imports that violate the manifest's directed layer policy."""
    forbidden: set[ImportEdge] = set()
    for edge in local_import_edges(root, manifest):
        source = manifest.modules[edge.source]
        target = manifest.modules[edge.target]
        if target.layer not in manifest.layers[source.layer]:
            forbidden.add(edge)
    return forbidden


def import_baseline_path() -> Path:
    """Return the checked-in compatibility baseline for legacy import pairs."""
    return Path(__file__).with_name("import-baseline.txt")


def load_import_baseline(manifest: ModuleManifest, path: Path | None = None) -> set[ImportEdge]:
    """Load legacy forbidden pairs, rejecting malformed or unknown entries."""
    source = path or import_baseline_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ModuleManifestError(f"Cannot read import baseline: {source}") from exc

    edges: set[ImportEdge] = set()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(" -> ")
        if len(parts) != 2 or not all(parts):
            raise ModuleManifestError(f"Invalid import baseline entry at line {line_number}: {line!r}")
        edge = ImportEdge(*parts)
        if edge.source not in manifest.modules or edge.target not in manifest.modules:
            raise ModuleManifestError(f"Unknown module in import baseline at line {line_number}: {line!r}")
        edges.add(edge)
    return edges


def _owned_python_files(root: Path, manifest: ModuleManifest) -> dict[Path, ModuleContract]:
    files: dict[Path, ModuleContract] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.parts[0] in {".venv", "tests"}:
            continue
        try:
            files[path] = manifest.module_for(relative)
        except ModuleManifestError:
            continue
    return files


def _import_targets(source_files: dict[Path, ModuleContract]) -> dict[str, ModuleContract]:
    targets: dict[str, ModuleContract] = {}
    for path, contract in source_files.items():
        parts = path.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        # The caller stores absolute paths, so discard repository path parts.
        for index, part in enumerate(parts):
            if part in {
                "agents",
                "approval",
                "architecture",
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
            }:
                targets[".".join(parts[index:])] = contract
                break
    return targets


def _package_for(relative: Path) -> tuple[str, ...]:
    parts = relative.with_suffix("").parts
    return parts[:-1] if parts[-1] != "__init__" else parts[:-1]


def _imported_names(tree: ast.AST, package: tuple[str, ...]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                names.add(".".join((*prefix, node.module)))
            else:
                names.add(node.module)
    return names


def _target_for(imported: str, targets: dict[str, ModuleContract]) -> ModuleContract | None:
    parts = imported.split(".")
    for end in range(len(parts), 0, -1):
        target = targets.get(".".join(parts[:end]))
        if target is not None:
            return target
    return None
