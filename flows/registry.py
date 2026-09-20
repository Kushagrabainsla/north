"""Discovery and validation for built-in and learned flows."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from flows.exceptions import FlowNotFoundError, FlowParseError
from flows.models import FLOW_FILENAME, Flow, FlowSource, FlowStep

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_APPROVALS = {"never", "always", "on_mutation"}


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FlowParseError(f"{label} must be a mapping")
    return value


def _parse_steps(raw: Any) -> tuple[FlowStep, ...]:
    if not isinstance(raw, list) or not raw:
        raise FlowParseError("steps must be a non-empty list")
    steps: list[FlowStep] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        data = _as_mapping(item, f"steps[{index}]")
        name = str(data.get("name") or "").strip()
        tool = str(data.get("tool") or "").strip()
        approval = str(data.get("approval") or "on_mutation").strip().lower()
        if not name:
            raise FlowParseError(f"steps[{index}] is missing name")
        if not tool:
            raise FlowParseError(f"steps[{index}] is missing tool")
        if name in seen:
            raise FlowParseError(f"duplicate step name: {name}")
        if approval not in _APPROVALS:
            raise FlowParseError(
                f"steps[{index}] has invalid approval {approval!r}; "
                f"expected one of {sorted(_APPROVALS)}"
            )
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise FlowParseError(f"steps[{index}].params must be a mapping")
        seen.add(name)
        steps.append(
            FlowStep(
                name=name,
                tool=tool,
                params=dict(params),
                skill=str(data.get("skill") or "").strip(),
                approval=approval,
                description=str(data.get("description") or "").strip(),
            )
        )
    return tuple(steps)


def parse_flow_document(text: str, directory: Path, source: FlowSource) -> Flow:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FlowParseError(f"invalid YAML: {exc}") from exc
    data = _as_mapping(raw, "flow document")
    name = str(data.get("name") or directory.name).strip()
    description = str(data.get("description") or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise FlowParseError("name must contain lowercase letters, numbers, and hyphens")
    if not description:
        raise FlowParseError("missing description")
    status = str(data.get("status") or "active").strip().lower()
    if status not in {"candidate", "active", "retired"}:
        raise FlowParseError(f"invalid status: {status}")
    raw_domains = data.get("domains")
    domains = (
        frozenset(str(item).strip() for item in raw_domains if str(item).strip())
        if isinstance(raw_domains, list) and raw_domains
        else frozenset({"general"})
    )
    provenance = tuple(str(item) for item in (data.get("provenance") or []))
    return Flow(
        name=name,
        description=description,
        steps=_parse_steps(data.get("steps")),
        directory=directory,
        source=source,
        version=str(data.get("version") or "1.0.0").strip(),
        status=status,
        domains=domains,
        provenance=provenance,
    )


class FlowRegistry:
    """Load built-ins first, then let user-owned flows override by name."""

    def __init__(self, builtin_dir: Path, learned_dir: Path | None = None) -> None:
        self._sources: list[tuple[Path, FlowSource]] = [(builtin_dir, FlowSource.BUILTIN)]
        if learned_dir is not None:
            self._sources.append((learned_dir, FlowSource.LEARNED))
        self._flows: dict[str, Flow] = {}
        self._discover()

    def _discover(self) -> None:
        self._flows = {}
        for directory, source in self._sources:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                flow_file = entry / FLOW_FILENAME
                if not (entry.is_dir() and flow_file.is_file()):
                    continue
                try:
                    flow = parse_flow_document(flow_file.read_text(encoding="utf-8"), entry, source)
                except (FlowParseError, OSError) as exc:
                    logger.warning("FlowRegistry: skipping %s - %s", entry.name, exc)
                    continue
                self._flows[flow.name] = flow

    def reload(self) -> None:
        self._discover()

    def get(self, name: str) -> Flow:
        if name not in self._flows:
            raise FlowNotFoundError(f"No flow registered with name: {name}")
        return self._flows[name]

    def all(self) -> list[Flow]:
        return list(self._flows.values())

    def names(self) -> list[str]:
        return list(self._flows)

    def remove_learned(self, name: str) -> bool:
        flow = self._flows.get(name)
        if flow is None or flow.source is not FlowSource.LEARNED:
            return False
        roots = [directory.resolve() for directory, source in self._sources if source is FlowSource.LEARNED]
        flow_dir = flow.directory.resolve()
        if not any(flow_dir.parent == root for root in roots):
            return False
        shutil.rmtree(flow_dir)
        self.reload()
        return True
