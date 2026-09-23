"""CreateSkillTool - create and hot-reload procedural skills at runtime.

Allows agents to package recurring procedures into reusable skills on the fly.
Writes SKILL.md to North's learned skills directory and reloads the SkillRegistry.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from policies.self_edit import SelfEditPolicy
from skills.models import SKILL_FILENAME, SkillIntent, SkillSource
from skills.parser import parse_skill_document
from skills.registry import parse_execution_contract, rejection_reason
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from skills.registry import SkillRegistry

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return s or "skill"


def _write_skill_file(skill_dir: Path, skill_file: Path, document: str) -> None:
    """Create the skill directory and write its SKILL.md. Blocking - use to_thread."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(document, encoding="utf-8")


class CreateSkillTool(Tool):
    """Create a new procedural skill (reusable step-by-step workflow) at runtime."""

    name = "create_skill"
    is_mutating = True
    description = (
        "Create, update, inspect, validate, and activate procedural skills. Use the "
        "authoring-a-north-skill skill first and reuse an existing procedure when possible. "
        "New and edited skills remain candidates until positive and negative selection tests pass "
        "and the user explicitly confirms activation."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "list", "read", "validate", "activate"],
                "default": "create",
            },
            "name": {
                "type": "string",
                "description": "Skill name (e.g. 'deploy-docker-container', 'debug-auth-error')",
            },
            "description": {
                "type": "string",
                "description": (
                    "Trigger condition starting with 'Use when...'. "
                    "E.g. 'Use when building and deploying a Docker container.'"
                ),
            },
            "instructions": {
                "type": "string",
                "description": "Markdown step-by-step instructions and best practices for completing this procedure.",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Agent domains that may receive the skill (default: general).",
            },
            "intents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional supported intent identifiers used by skill selection.",
            },
            "execution": {
                "type": "object",
                "description": (
                    "Optional flow-execution contract containing agent, exact tools, input/output object "
                    "schemas, minimum approval, and success_criteria. Required before a flow can use the skill."
                ),
            },
            "positive_prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Representative prompts that must select this skill during validation.",
            },
            "negative_prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Nearby prompts that must not select this skill during validation.",
            },
            "user_confirmed": {
                "type": "boolean",
                "description": "True only after the user explicitly approves activation.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        registry: SkillRegistry,
        learned_dir: Path | None = None,
        self_edit_policy: SelfEditPolicy | None = None,
        skill_selector=None,
        tool_registry=None,
        agent_registry=None,
    ) -> None:
        self._registry = registry
        self._learned_dir = learned_dir or (Path.home() / ".north" / "skills")
        self._self_edit_policy = self_edit_policy
        self._skill_selector = skill_selector
        self._tool_registry = tool_registry
        self._agent_registry = agent_registry
        self._validated: dict[str, str] = {}

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return str((params or {}).get("action") or "create").strip().lower() in {
            "create",
            "update",
            "activate",
        }

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action") or "create").strip().lower()
        if action == "list":
            return ToolOutput(
                success=True,
                data={
                    "skills": [
                        {
                            "name": skill.name,
                            "description": skill.description,
                            "status": skill.status,
                            "domains": sorted(skill.domains),
                            "executable": skill.execution is not None,
                        }
                        for skill in sorted(self._registry.all(), key=lambda item: item.name)
                    ]
                },
            )
        if action == "read":
            return self._read(input.params)
        if action == "validate":
            return await self._validate(input.params)
        if action == "activate":
            return await self._activate(input.params)
        if action not in {"create", "update"}:
            return ToolOutput(success=False, error="Unknown create_skill action.")

        raw_name = str(input.params.get("name") or "").strip()
        description = str(input.params.get("description") or "").strip()
        instructions = str(input.params.get("instructions") or "").strip()

        if not raw_name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        if not description:
            return ToolOutput(success=False, error="Parameter 'description' is required.")
        if not instructions:
            return ToolOutput(success=False, error="Parameter 'instructions' is required.")

        slug_name = _slug(raw_name)
        skill_dir = self._learned_dir / slug_name
        skill_file = skill_dir / SKILL_FILENAME

        if not description.startswith("Use when"):
            return ToolOutput(success=False, error="Description must start with 'Use when'.")
        if action == "create" and slug_name in self._registry.names():
            return ToolOutput(success=False, error=f"Skill '{slug_name}' already exists. Update it instead.")
        if action == "update":
            try:
                self._registry.get(slug_name)
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))

        domains = input.params.get("domains") or ["general"]
        intents = input.params.get("intents") or []
        if not isinstance(domains, list) or not all(isinstance(item, str) and item.strip() for item in domains):
            return ToolOutput(success=False, error="Parameter 'domains' must be a non-empty list of strings.")
        if not isinstance(intents, list) or not all(isinstance(item, str) for item in intents):
            return ToolOutput(success=False, error="Parameter 'intents' must be a list of strings.")
        unknown_intents = {item.strip().lower() for item in intents} - {item.value for item in SkillIntent}
        if unknown_intents:
            return ToolOutput(success=False, error=f"Unknown skill intents: {sorted(unknown_intents)}")

        execution_raw = input.params.get("execution")
        try:
            execution = parse_execution_contract(execution_raw)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Invalid execution contract: {exc}")
        if execution is not None:
            contract_error = self._execution_error(execution, domains)
            if contract_error:
                return ToolOutput(success=False, error=contract_error)

        metadata = {
            "name": slug_name,
            "description": description,
            "source": SkillSource.LEARNED.value,
            "status": "candidate",
            "domains": domains,
            "intents": intents,
        }
        if execution is not None:
            metadata["execution"] = execution_raw
        frontmatter = yaml.safe_dump(
            metadata,
            sort_keys=False,
            allow_unicode=True,
        )
        document = f"---\n{frontmatter}---\n\n{instructions}\n"
        parsed_frontmatter, body = parse_skill_document(document)
        reason = rejection_reason(slug_name, description, body, source=SkillSource.LEARNED)
        if reason:
            return ToolOutput(success=False, error=f"Skill is invalid: {reason}")

        try:
            operation = "update" if skill_file.exists() else "create"
            mutation = (
                self._self_edit_policy.begin(skill_file, operation)
                if self._self_edit_policy is not None
                else None
            )
            # mkdir + write off-thread so the agent loop is never blocked on disk.
            await asyncio.to_thread(_write_skill_file, skill_dir, skill_file, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            if hasattr(self._registry, "reload"):
                self._registry.reload()
            loaded = self._registry.get(slug_name)
            if loaded.status != "candidate":
                return ToolOutput(success=False, error=f"Skill '{slug_name}' did not load as a candidate.")
            self._validated.pop(slug_name, None)
            return ToolOutput(
                success=True,
                data={
                    "name": slug_name,
                    "description": description,
                    "path": str(skill_file),
                    "status": loaded.status,
                    "updated": action == "update",
                    "frontmatter": parsed_frontmatter,
                },
            )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to create skill '{slug_name}': {exc}")

    def _read(self, params: dict[str, Any]) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for read.")
        try:
            skill = self._registry.get(name)
            content = (skill.directory / SKILL_FILENAME).read_text(encoding="utf-8")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        return ToolOutput(
            success=True,
            data={"name": name, "content": content, "status": skill.status},
        )

    async def _validate(self, params: dict[str, Any]) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        positives = [str(item).strip() for item in params.get("positive_prompts") or [] if str(item).strip()]
        negatives = [str(item).strip() for item in params.get("negative_prompts") or [] if str(item).strip()]
        if not name or not positives or not negatives:
            return ToolOutput(
                success=False,
                error="Validation requires name, positive_prompts, and negative_prompts.",
            )
        if self._skill_selector is None:
            return ToolOutput(success=False, error="Skill selection testing is unavailable.")
        try:
            skill = self._registry.get(name)
            candidates = self._registry.all()
            positive_failures = [
                prompt
                for prompt in positives
                if name not in {item.name for item in await self._skill_selector.select(prompt, candidates=candidates)}
            ]
            negative_failures = [
                prompt
                for prompt in negatives
                if name in {item.name for item in await self._skill_selector.select(prompt, candidates=candidates)}
            ]
        except Exception as exc:
            return ToolOutput(success=False, error=f"Skill validation failed: {exc}")
        if positive_failures or negative_failures:
            return ToolOutput(
                success=False,
                error="Skill selection tests failed.",
                data={
                    "positive_failures": positive_failures,
                    "negative_failures": negative_failures,
                },
            )
        fingerprint = _skill_fingerprint(skill.directory / SKILL_FILENAME)
        self._validated[name] = fingerprint
        return ToolOutput(
            success=True,
            data={"name": name, "valid": True, "positive_tests": len(positives), "negative_tests": len(negatives)},
        )

    async def _activate(self, params: dict[str, Any]) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for activate.")
        if params.get("user_confirmed") is not True:
            return ToolOutput(success=False, error="Activation requires explicit user confirmation.")
        try:
            skill = self._registry.get(name)
            path = skill.directory / SKILL_FILENAME
            fingerprint = _skill_fingerprint(path)
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        if self._validated.get(name) != fingerprint:
            return ToolOutput(
                success=False,
                error="Validate the current skill with positive and negative prompts first.",
            )
        if skill.status == "active":
            return ToolOutput(success=True, data={"name": name, "status": "active"})
        if skill.status != "candidate":
            return ToolOutput(success=False, error=f"Skill '{name}' is {skill.status}, not a candidate.")
        if skill.execution is not None and (
            contract_error := self._execution_error(skill.execution, skill.domains)
        ):
            return ToolOutput(success=False, error=contract_error)

        content = path.read_text(encoding="utf-8")
        frontmatter, body = parse_skill_document(content)
        frontmatter["status"] = "active"
        document = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n{body.strip()}\n"
        try:
            mutation = (
                self._self_edit_policy.begin(path, "update")
                if self._self_edit_policy is not None
                else None
            )
            await asyncio.to_thread(_write_skill_file, skill.directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(success=True, data={"name": name, "status": "active"})
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to activate skill '{name}': {exc}")

    def _execution_error(self, execution, domains) -> str:
        if self._agent_registry is not None:
            try:
                agent = self._agent_registry.get(execution.agent)
            except Exception:
                return f"Execution contract names unknown agent '{execution.agent}'."
            if agent.domain not in domains:
                return (
                    f"Execution agent '{execution.agent}' has domain '{agent.domain}', which is not "
                    "listed in the skill domains."
                )
        if self._tool_registry is not None:
            for name in execution.tools:
                if name.startswith("$input."):
                    continue
                try:
                    self._tool_registry.get(name)
                except Exception:
                    return f"Execution contract names unknown tool '{name}'."
        return ""

    def format_output(self, data: dict[str, Any]) -> str:
        if "skills" in data:
            return "\n".join(
                f"{item['name']} - {item['description']} ({item['status']})"
                for item in data["skills"]
            ) or "No skills registered."
        if "content" in data:
            return str(data["content"])
        if data.get("valid"):
            return f"Skill '{data.get('name')}' passed its selection tests."
        if data.get("status") == "active":
            return f"Skill '{data.get('name')}' is active."
        verb = "updated" if data.get("updated") else "created"
        return (
            f"Skill candidate '{data.get('name')}' {verb} at {data.get('path')}. "
            "It is not active until selection tests pass and the user confirms activation."
        )


def _skill_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
