"""Server-owned policy for North-authored source mutations.

The model may propose a mutation, but it cannot decide whether the target is
editable.  This policy keeps the consent boundary, sandbox definition, ledger,
inference, configuration, and credentials outside North's self-edit surface.
All other updates require an authorship record created by this module.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path

_PROTECTED = (
    "approval/**",
    "tools/_path.py",
    "ledger/**",
    "inference/**",
    "config/**",
    "credentials/**",
    ".env",
    ".env.*",
)


@dataclass(frozen=True)
class Mutation:
    id: str
    path: str
    operation: str
    before_image: str | None


class SelfEditPolicy:
    """Authorize and journal create/update mutations for North-owned paths."""

    def __init__(self, root: Path, registry_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.registry_root = (registry_root or (Path.home() / ".north" / "mutations")).resolve()
        self.registry_root.mkdir(parents=True, exist_ok=True)
        self._registry = self.registry_root / "authorship.json"
        self._snapshots = self.registry_root / "before"
        self._snapshots.mkdir(exist_ok=True)

    def authorize(self, path: Path, operation: str) -> str | None:
        """Return a refusal reason, or ``None`` when the mutation is allowed."""
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError:
            return "Self-edit target is outside the managed root."

        if any(fnmatchcase(relative, pattern) for pattern in _PROTECTED):
            return f"Path `{relative}` is frozen by the self-edit policy."
        if operation == "create":
            if resolved.exists():
                return f"Path `{relative}` already exists; North may only create new files."
            return None
        if operation == "update":
            if not self._owned(resolved):
                return f"Path `{relative}` was not created by North."
            return None
        return f"Unsupported self-edit operation: {operation!r}."

    def begin(self, path: Path, operation: str) -> Mutation:
        refusal = self.authorize(path, operation)
        if refusal:
            raise PermissionError(refusal)
        mutation_id = uuid.uuid4().hex
        before_image: str | None = None
        if operation == "update":
            before_image = str(self._snapshots / mutation_id)
            shutil.copy2(path, before_image)
        return Mutation(mutation_id, str(path.resolve()), operation, before_image)

    def commit(self, mutation: Mutation) -> None:
        path = Path(mutation.path)
        records = self._read_registry()
        record = {
            "id": mutation.id,
            "path": mutation.path,
            "operation": mutation.operation,
            "before_image": mutation.before_image,
            "sha256": _sha256(path) if path.is_file() else None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        records.append(record)
        self._registry.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    def revert(self, mutation_id: str) -> bool:
        records = self._read_registry()
        for record in reversed(records):
            if record.get("id") != mutation_id:
                continue
            path = Path(str(record["path"]))
            before = record.get("before_image")
            if before:
                shutil.copy2(before, path)
            else:
                path.unlink(missing_ok=True)
            return True
        return False

    def _owned(self, path: Path) -> bool:
        return any(record.get("path") == str(path.resolve()) for record in self._read_registry())

    def _read_registry(self) -> list[dict[str, object]]:
        if not self._registry.exists():
            return []
        try:
            value = json.loads(self._registry.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
