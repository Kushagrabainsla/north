"""CreateSkillTool - create and hot-reload procedural skills at runtime.

Allows agents to package recurring procedures into reusable skills on the fly.
Writes SKILL.md to North's learned skills directory and reloads the SkillRegistry.

One card per change: creating, updating or activating a skill is asked about
once, and that approval covers testing it and switching it on. The new version
is tested before anything is written, so one that fails never replaces a live skill.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from policies.self_edit import SelfEditPolicy
from skills.exceptions import SkillParseError
from skills.models import SKILL_FILENAME, SkillIntent, SkillSource
from skills.parser import parse_skill_document
from skills.registry import parse_execution_contract, rejection_reason, skill_from_document
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._procedure_edit import begin_edit, describe_user_edit

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
        "Create, update, inspect, or activate procedural skills. Use the authoring-a-north-skill skill "
        "first and reuse an existing procedure when possible. create, update and activate each take "
        "positive_prompts and negative_prompts: the user is asked once, then the skill is tested with them "
        "and is live if they pass. A failing new version changes nothing."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "activate", "list", "read"],
                "default": "create",
                "description": "activate = test a skill that is still a draft, as it is, and switch it on.",
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
                "items": {"type": "string", "enum": [intent.value for intent in SkillIntent]},
                "description": "Optional fixed intent identifiers, not free-form triggers. Omit when none fits.",
            },
            "execution": {
                "type": "object",
                "description": (
                    "Optional flow-execution contract containing agent, exact tools, input/output object "
                    "schemas, minimum approval, and success_criteria. Required before a flow can use the skill."
                ),
                "properties": {
                    "agent": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "approval": {"type": "string", "enum": ["never", "on_mutation", "before_step"]},
                    "inputs": {"type": "object", "description": "JSON object schema for required runtime inputs."},
                    "outputs": {"type": "object", "description": "JSON object schema for the final result."},
                    "success_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["agent", "tools", "approval", "inputs", "outputs", "success_criteria"],
                "additionalProperties": False,
            },
            "positive_prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Requests that must select this skill. Required for create, update, activate.",
            },
            "negative_prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Nearby requests that must not select this skill. Required for create/update/activate.",
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

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return str((params or {}).get("action") or "create").strip().lower() in {
            "create",
            "update",
            "activate",
        }

    async def describe(self, input: ToolInput):
        """No card for a call this tool will refuse anyway: asking first only wastes your approval.

        A write the self-edit policy forbids, or an activation that was never validated or confirmed, is
        refused by `run` without changing anything. Asking you about it first produced four cards for four
        refused updates to a skill North had not written.
        """
        if self._will_refuse(input.params):
            return None
        request = await super().describe(input)
        action = str(input.params.get("action") or "create").strip().lower()
        name = _slug(str(input.params.get("name") or ""))
        verb = {"create": "creates", "update": "updates", "activate": "tests"}.get(action, action)
        request = replace(
            request,
            message=(
                f"Approving {verb} the skill '{name}', checks it is picked for the example requests and not "
                "the others, and switches it on if that passes. If it fails, nothing changes.\n\n" + request.message
            ),
        )
        if action == "update":
            path = self._learned_dir / _slug(str(input.params.get("name") or "")) / SKILL_FILENAME
            return describe_user_edit(self._self_edit_policy, path, input, request)
        return request

    def _will_refuse(self, params: dict[str, Any]) -> bool:
        action = str(params.get("action") or "create").strip().lower()
        name = _slug(str(params.get("name") or "").strip())
        if not name:
            return False
        path = self._learned_dir / name / SKILL_FILENAME
        if action == "activate":
            # Nothing to ask about: no such skill, or it is live already.
            try:
                return self._registry.get(name).status != "candidate"
            except Exception:
                return True
        if action in {"create", "update"} and self._self_edit_policy is not None:
            if action == "update" and not self._self_edit_policy.authorize(path, "user_update"):
                return False
            return self._self_edit_policy.authorize(path, "update" if path.exists() else "create") is not None
        return False

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
            return ToolOutput(
                success=False,
                error=f"Unknown skill intents: {sorted(unknown_intents)}. "
                f"Use only {[intent.value for intent in SkillIntent]}, or omit intents.",
            )

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
            "status": "active",
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
            skill = skill_from_document(document, skill_dir, SkillSource.LEARNED)
        except SkillParseError as exc:
            return ToolOutput(success=False, error=f"Skill is invalid: {exc}")
        if (failed := await self._selection_failures(skill, input.params)) is not None:
            return failed

        try:
            operation = "update" if skill_file.exists() else "create"
            mutation = (
                begin_edit(self._self_edit_policy, skill_file, operation, input) if self._self_edit_policy else None
            )
            # mkdir + write off-thread so the agent loop is never blocked on disk.
            await asyncio.to_thread(_write_skill_file, skill_dir, skill_file, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            if hasattr(self._registry, "reload"):
                self._registry.reload()
            loaded = self._registry.get(slug_name)
            if loaded.status != "active":
                return ToolOutput(success=False, error=f"Skill '{slug_name}' did not load as live.")
            return ToolOutput(
                success=True,
                data={
                    "name": slug_name,
                    "description": description,
                    "path": str(skill_file),
                    "status": loaded.status,
                    "updated": action == "update",
                    "frontmatter": parsed_frontmatter,
                    **_tests_run(input.params),
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

    async def _selection_failures(self, skill, params: dict[str, Any]) -> ToolOutput | None:
        """Why *skill* fails its selection tests, or None when it passes.

        It is tested as it would be live: against every other skill, in place of
        any version of itself, before anything is written.
        """
        positives, negatives = _prompts(params, "positive_prompts"), _prompts(params, "negative_prompts")
        if not positives or not negatives:
            return ToolOutput(
                success=False,
                error="positive_prompts and negative_prompts are required: the skill is tested with them "
                "before it is switched on.",
            )
        if self._skill_selector is None:
            return ToolOutput(success=False, error="Skill selection testing is unavailable.")
        candidates = [item for item in self._registry.all() if item.name != skill.name] + [skill]

        async def picked(prompt: str) -> bool:
            chosen = await self._skill_selector.select(prompt, candidates=candidates)
            return skill.name in {item.name for item in chosen}

        try:
            missed = [prompt for prompt in positives if not await picked(prompt)]
            wrongly = [prompt for prompt in negatives if await picked(prompt)]
        except Exception as exc:
            return ToolOutput(success=False, error=f"Skill selection testing failed: {exc}")
        if missed or wrongly:
            return ToolOutput(
                success=False,
                error="Skill selection tests failed, so nothing changed.",
                data={"positive_failures": missed, "negative_failures": wrongly},
            )
        return None

    async def _activate(self, params: dict[str, Any]) -> ToolOutput:
        """Test a draft skill as it is, and switch it on if it passes."""
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for activate.")
        try:
            skill = self._registry.get(name)
            path = skill.directory / SKILL_FILENAME
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))
        if skill.status == "active":
            return ToolOutput(success=True, data={"name": name, "status": "active"})
        if skill.status != "candidate":
            return ToolOutput(success=False, error=f"Skill '{name}' is {skill.status}, not a draft.")
        if skill.execution is not None and (contract_error := self._execution_error(skill.execution, skill.domains)):
            return ToolOutput(success=False, error=contract_error)
        live = replace(skill, status="active")
        if (failed := await self._selection_failures(live, params)) is not None:
            return failed

        frontmatter, body = parse_skill_document(content)
        frontmatter["status"] = "active"
        document = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n{body.strip()}\n"
        try:
            mutation = self._self_edit_policy.begin(path, "activate") if self._self_edit_policy is not None else None
            await asyncio.to_thread(_write_skill_file, skill.directory, path, document)
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.reload()
            return ToolOutput(success=True, data={"name": name, "status": "active", **_tests_run(params)})
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
            return (
                "\n".join(f"{item['name']} - {item['description']} ({item['status']})" for item in data["skills"])
                or "No skills registered."
            )
        if "content" in data:
            return str(data["content"])
        tested = (
            f" It was picked for {data['positive_tests']} example request(s) and none of "
            f"{data['negative_tests']} others."
            if "positive_tests" in data
            else ""
        )
        if data.get("path"):
            verb = "updated" if data.get("updated") else "created"
            return f"Skill '{data.get('name')}' {verb} at {data.get('path')} and live.{tested}"
        return f"Skill '{data.get('name')}' is live.{tested}"


def _prompts(params: dict[str, Any], key: str) -> list[str]:
    return [str(item).strip() for item in params.get(key) or [] if str(item).strip()]


def _tests_run(params: dict[str, Any]) -> dict[str, int]:
    return {
        "positive_tests": len(_prompts(params, "positive_prompts")),
        "negative_tests": len(_prompts(params, "negative_prompts")),
    }
