"""Tool that creates new north agents at runtime from a natural-language description.

The calling agent (typically general) generates the config, accepts list, and
system prompt, then hands them to this tool which writes the files and triggers
a live reload. The new agent is immediately available for routing without any
server restart.

Mirrors the design of create_tool.py — see docs/CODING_STYLE.md Section 16.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from agents.registry import AgentRegistry
    from jobs.cron_store import UserCronStore

logger = logging.getLogger(__name__)

_AGENTS_ROOT = Path(__file__).parent.parent.parent / "agents"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_AGENT_PY_TEMPLATE = '''\
"""Auto-generated agent — {name}."""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class {class_name}(AgenticLLMAgent):
    """{description}"""
'''

_CONFIG_TEMPLATE = """\
agent: {name}
domain: {domain}
model_pool: {model_pool}
accepts:
{accepts_block}
output_format: structured_json
version: 1.0.0
class_name: {class_name}
"""


def _to_class_name(snake: str) -> str:
    return "".join(w.capitalize() for w in snake.split("_")) + "Agent"


class CreateAgentTool(Tool):
    """Creates a new north agent at runtime from a description and system prompt.

    The calling agent generates the system_prompt content, accepts keywords,
    and domain — this tool writes the files and hot-loads the result so the
    new agent is immediately available for routing without a server restart.
    """

    name = "create_agent"
    description = (
        "Creates a new north agent from a natural-language description. "
        "action='list': show all registered agents. "
        "action='read': return an agent's config and system prompt. "
        "action='create': write agent files and hot-load — the new agent is available immediately. "
        "You must supply name (snake_case), description, accepts (routing keywords), "
        "domain, and system_prompt when creating."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "read"],
                "description": (
                    "list = show all agents; "
                    "read = return config + system prompt for an agent by name; "
                    "create = write agent files and hot-load."
                ),
            },
            "name": {
                "type": "string",
                "description": "Agent name in snake_case (e.g. 'weather_checker'). Required for create and read.",
            },
            "description": {
                "type": "string",
                "description": "One sentence: what this agent does. Required for create.",
            },
            "domain": {
                "type": "string",
                "description": "Agent domain: general, health, engineering, finance, news, etc. Default: general.",
            },
            "model_pool": {
                "type": "string",
                "enum": ["fast_cheap", "reasoning", "high_volume"],
                "description": "Model tier to use. Default: fast_cheap (sufficient for most agents).",
            },
            "accepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of keywords / short phrases the orchestrator uses to route tasks here. "
                    "Include synonyms and topic words so the LLM router can match user requests. "
                    "Example: ['weather', 'forecast', 'temperature', 'rain']"
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": (
                    "Full markdown content for prompts/system.md. "
                    "Write it as you would write a north agent system prompt: "
                    "role, what it owns, step-by-step workflow, output format, rules."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        agent_registry: AgentRegistry | None = None,
        cron_store: UserCronStore | None = None,
    ) -> None:
        self._agent_registry = agent_registry  # wired after registry is built (see app.py)
        self._cron_store = cron_store

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action")

        if action == "list":
            rows = data.get("agents", [])
            if not rows:
                return "No agents registered."
            lines = []
            for r in rows:
                accepts_preview = ", ".join(r.get("accepts", [])[:4])
                lines.append(f"[{r['domain']}] {r['name']} — {r['description']} (accepts: {accepts_preview}…)")
            return "\n".join(lines)

        if action == "read":
            out = [f"=== {data['name']} ===", "", "--- config.yaml ---", data.get("config", ""), "",
                   "--- prompts/system.md ---", data.get("system_prompt", "")]
            return "\n".join(out)

        if action == "create":
            lines = [f"Agent created: {data['path']}"]
            if data.get("hot_loaded"):
                lines.append("Hot-loaded — the agent is available immediately for routing.")
            else:
                lines.append("Files written. Agent will be auto-discovered on the next request.")
            return "\n".join(lines)

        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = (input.params.get("action") or "").strip()

        if action == "list":
            return _list_agents()
        if action == "read":
            return _read_agent(input.params.get("name") or "")
        if action == "create":
            return self._create(input.params)

        return ToolOutput(success=False, error=f"Unknown action '{action}'. Use: list, read, create.")

    def _create(self, params: dict) -> ToolOutput:
        name = (params.get("name") or "").strip().lower()
        description = (params.get("description") or "").strip()
        domain = (params.get("domain") or "general").strip().lower()
        model_pool = (params.get("model_pool") or "fast_cheap").strip()
        accepts: list[str] = params.get("accepts") or []
        system_prompt = (params.get("system_prompt") or "").strip()

        if not name:
            return ToolOutput(success=False, error="'name' is required for action=create.")
        if not _NAME_RE.match(name):
            return ToolOutput(
                success=False,
                error="Agent name must be snake_case (lowercase letters, digits, underscores).",
            )
        if not description:
            return ToolOutput(success=False, error="'description' is required for action=create.")
        if not accepts:
            return ToolOutput(success=False, error="'accepts' (list of routing keywords) is required for action=create.")
        if not system_prompt:
            return ToolOutput(success=False, error="'system_prompt' (prompts/system.md content) is required for action=create.")
        if model_pool not in ("fast_cheap", "reasoning", "high_volume"):
            model_pool = "fast_cheap"

        agent_dir = _AGENTS_ROOT / name
        if agent_dir.exists():
            return ToolOutput(
                success=False,
                error=(
                    f"Agent '{name}' already exists at agents/{name}/. "
                    "Use action='read' to inspect it."
                ),
            )

        class_name = _to_class_name(name)
        accepts_block = "".join(f'  - "{a}"\n' for a in accepts)

        try:
            agent_dir.mkdir(parents=True, exist_ok=False)
            (agent_dir / "__init__.py").write_text("", encoding="utf-8")

            (agent_dir / "config.yaml").write_text(
                _CONFIG_TEMPLATE.format(
                    name=name,
                    domain=domain,
                    model_pool=model_pool,
                    accepts_block=accepts_block.rstrip(),
                    class_name=class_name,
                ),
                encoding="utf-8",
            )

            (agent_dir / "agent.py").write_text(
                _AGENT_PY_TEMPLATE.format(
                    name=name,
                    class_name=class_name,
                    description=description,
                ),
                encoding="utf-8",
            )

            prompts_dir = agent_dir / "prompts"
            prompts_dir.mkdir()
            (prompts_dir / "system.md").write_text(system_prompt, encoding="utf-8")

        except OSError as exc:
            return ToolOutput(success=False, error=f"Failed to write agent files: {exc}")

        hot_loaded = self._hot_load(name)

        return ToolOutput(
            success=True,
            data={
                "action": "create",
                "name": name,
                "path": f"agents/{name}/",
                "hot_loaded": hot_loaded,
            },
        )

    def _hot_load(self, name: str) -> bool:
        if self._agent_registry is None:
            return False
        try:
            new_names = self._agent_registry.reload()
            return name in new_names
        except Exception:
            logger.warning("CreateAgentTool: hot-load of %r failed", name, exc_info=True)
            return False


# ── Standalone helpers ────────────────────────────────────────────────────────

def _list_agents() -> ToolOutput:
    rows: list[dict] = []
    if not _AGENTS_ROOT.exists():
        return ToolOutput(success=True, data={"action": "list", "agents": rows})
    for entry in sorted(_AGENTS_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        config_path = entry / "config.yaml"
        if not config_path.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            rows.append({
                "name": data.get("agent", entry.name),
                "domain": data.get("domain", "general"),
                "description": (entry / "prompts" / "system.md").read_text(encoding="utf-8")[:120].split("\n")[0].lstrip("# ").strip()
                if (entry / "prompts" / "system.md").exists() else "",
                "accepts": data.get("accepts") or [],
            })
        except Exception:
            continue
    return ToolOutput(success=True, data={"action": "list", "agents": rows})


def _read_agent(name: str) -> ToolOutput:
    if not name.strip():
        return ToolOutput(success=False, error="'name' is required for action=read.")
    agent_dir = _AGENTS_ROOT / name.strip()
    if not agent_dir.exists():
        return ToolOutput(success=False, error=f"Agent '{name}' not found at agents/{name}/.")
    config_text = (agent_dir / "config.yaml").read_text(encoding="utf-8") if (agent_dir / "config.yaml").exists() else ""
    prompt_text = (agent_dir / "prompts" / "system.md").read_text(encoding="utf-8") if (agent_dir / "prompts" / "system.md").exists() else ""
    return ToolOutput(
        success=True,
        data={
            "action": "read",
            "name": name,
            "config": config_text,
            "system_prompt": prompt_text,
        },
    )
