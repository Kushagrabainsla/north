"""Load and validate North's version-controlled module contracts."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml


class ModuleManifestError(ValueError):
    """The module manifest is malformed or cannot uniquely classify a path."""


@dataclass(frozen=True)
class ModuleContract:
    """Ownership and path rules for one architectural module."""

    name: str
    paths: tuple[str, ...]
    excludes: tuple[str, ...]
    layer: str
    owner: str
    editable_by: tuple[str, ...]
    protected: bool
    validation: tuple[str, ...]

    def matches(self, path: str | Path) -> bool:
        """Whether this contract owns a repository-relative path."""
        relative = Path(path).as_posix().lstrip("./")
        return any(fnmatchcase(relative, pattern) for pattern in self.paths) and not any(
            fnmatchcase(relative, pattern) for pattern in self.excludes
        )


@dataclass(frozen=True)
class ModuleManifest:
    """Validated architectural module catalog."""

    layers: dict[str, tuple[str, ...]]
    modules: dict[str, ModuleContract]

    def module_for(self, path: str | Path) -> ModuleContract:
        """Return the sole contract that owns a repository-relative path."""
        matches = [contract for contract in self.modules.values() if contract.matches(path)]
        if len(matches) != 1:
            names = ", ".join(contract.name for contract in matches) or "none"
            raise ModuleManifestError(f"Expected exactly one module for {path!s}; found {names}.")
        return matches[0]


def manifest_path() -> Path:
    """Return the repository-shipped module manifest path."""
    return Path(__file__).with_name("modules.yaml")


def load_module_manifest(path: Path | None = None) -> ModuleManifest:
    """Load the module manifest and reject malformed contracts early."""
    source = path or manifest_path()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModuleManifestError(f"Cannot read module manifest: {source}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ModuleManifestError("Module manifest must be a mapping with version: 1.")

    layers_raw = _mapping(raw.get("layers"), "layers")
    layers = {
        name: tuple(_strings(value.get("may_import"), f"layers.{name}.may_import"))
        for name, value in layers_raw.items()
    }
    modules_raw = _mapping(raw.get("modules"), "modules")
    modules = {name: _contract(name, _mapping(value, f"modules.{name}"), layers) for name, value in modules_raw.items()}
    if not modules:
        raise ModuleManifestError("Module manifest must declare at least one module.")
    return ModuleManifest(layers=layers, modules=modules)


def _contract(name: str, raw: dict[str, Any], layers: dict[str, tuple[str, ...]]) -> ModuleContract:
    required = ("paths", "layer", "owner", "editable_by", "public_contracts", "validation")
    missing = [field for field in required if field not in raw]
    if missing:
        raise ModuleManifestError(f"modules.{name} is missing: {', '.join(missing)}.")
    layer = _string(raw["layer"], f"modules.{name}.layer")
    if layer not in layers:
        raise ModuleManifestError(f"modules.{name}.layer names unknown layer {layer!r}.")
    return ModuleContract(
        name=name,
        paths=tuple(_strings(raw["paths"], f"modules.{name}.paths")),
        excludes=tuple(_strings(raw.get("excludes", []), f"modules.{name}.excludes")),
        layer=layer,
        owner=_string(raw["owner"], f"modules.{name}.owner"),
        editable_by=tuple(_strings(raw["editable_by"], f"modules.{name}.editable_by")),
        protected=bool(raw.get("protected", False)),
        validation=tuple(_strings(raw["validation"], f"modules.{name}.validation")),
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModuleManifestError(f"{name} must be a mapping.")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModuleManifestError(f"{name} must be a non-empty string.")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ModuleManifestError(f"{name} must be a list of non-empty strings.")
    return value
