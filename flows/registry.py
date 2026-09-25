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


def _parse_steps(raw: Any, *, allow_actions: bool = False) -> tuple[tuple[FlowStep, ...], bool]:
    if not isinstance(raw, list) or not raw:
        raise FlowParseError("steps must be a non-empty list")
    steps: list[FlowStep] = []
    migrated_legacy_step = False
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        data = _as_mapping(item, f"steps[{index}]")
        name = str(data.get("name") or "").strip()
        skill = str(data.get("skill") or "").strip()
        action = str(data.get("action") or "").strip()
        if action and not allow_actions:
            raise FlowParseError(f"steps[{index}].action is only available to built-in flows")
        legacy_tool = str(data.get("tool") or "").strip()
        if data.get("agent") is not None:
            # Executor ownership moved to the skill contract. Keep old files
            # readable, but require an explicit save/test before execution.
            migrated_legacy_step = True
        raw_approval = data.get("approval", "on_mutation")
        if isinstance(raw_approval, bool):
            if not legacy_tool:
                raise FlowParseError(
                    f"steps[{index}].approval must be one of {sorted(_APPROVALS)}, not a boolean"
                )
            # Old flows used booleans before mutation-aware modes existed. A
            # false value cannot safely mean "never ask", so migrate it to the
            # per-mutation gate; true keeps the stronger whole-step gate.
            approval = "always" if raw_approval else "on_mutation"
            migrated_legacy_step = True
        else:
            approval = str(raw_approval or "on_mutation").strip().lower()
        if not name:
            raise FlowParseError(f"steps[{index}] is missing name")
        # A step may either reference a reusable skill or provide its own
        # inline instructions. The latter is executed by the general agent.
        if name in seen:
            raise FlowParseError(f"duplicate step name: {name}")
        if approval not in _APPROVALS:
            raise FlowParseError(
                f"steps[{index}] has invalid approval {approval!r}; "
                f"expected one of {sorted(_APPROVALS)}"
            )
        raw_inputs = data.get("inputs", data.get("params")) or {}
        if not isinstance(raw_inputs, dict):
            raise FlowParseError(f"steps[{index}].inputs must be a mapping")

        # Read old on-disk flows without executing their tools directly. They
        # become ordinary skill invocations in memory and are written in the new
        # format the next time the user edits them.
        if legacy_tool:
            migrated_legacy_step = True
            skill = "using-a-north-tool"
            legacy_instruction = f"Call the North tool '{legacy_tool}' with the supplied inputs."
            supplied = str(data.get("instructions") or data.get("description") or "").strip()
            instructions = f"{supplied}\n\n{legacy_instruction}".strip()
            raw_inputs = {"tool": legacy_tool, "arguments": dict(raw_inputs)}
        else:
            instructions = str(data.get("instructions") or data.get("description") or "").strip()
        # A skill-backed step needs no instructions of its own - the skill's body
        # is the procedure. Only a step with no skill (inline instructions are
        # its entire procedure) must have them. flows/validation.py
        # already enforces exactly this rule at the semantic-validation layer;
        # requiring instructions unconditionally here made the parser reject a
        # skill step with none before validation ever ran, which is also what
        # the dashboard's flow editor now saves for a step whose kind is
        # "existing skill" (see web/src/pages/Verbose.tsx FlowStepCard).
        if not instructions and not skill and not action:
            raise FlowParseError(f"steps[{index}] is missing instructions")
        seen.add(name)
        steps.append(
            FlowStep(
                name=name,
                skill=skill,
                instructions=instructions,
                inputs=dict(raw_inputs),
                approval=approval,
                action=action,
            )
        )
    return tuple(steps), migrated_legacy_step


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
    steps, migrated_legacy_step = _parse_steps(data.get("steps"), allow_actions=source is FlowSource.BUILTIN)
    status = str(data.get("status") or "active").strip().lower()
    if status not in {"candidate", "active", "retired"}:
        raise FlowParseError(f"invalid status: {status}")
    if migrated_legacy_step and status != "retired":
        # Loading is non-destructive: expose the migrated shape in the UI but
        # require an explicit save, test, and activation before it can execute.
        status = "candidate"
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
        steps=steps,
        directory=directory,
        source=source,
        status=status,
        domains=domains,
        provenance=provenance,
        activation_fingerprint=str(data.get("activation_fingerprint") or "").strip(),
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
