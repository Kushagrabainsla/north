"""Dynamic Platform Capabilities Synthesizer.

Inspects runtime registries (AgentRegistry, ToolRegistry, SkillRegistry) to build
an up-to-date summary of North's platform capabilities. Eliminates prompt hardcoding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.models import AgentDependencies


def build_platform_capabilities_summary(deps: AgentDependencies) -> str:
    """Build a dynamic summary of all tools, agents, and skills registered in North.

    Queries runtime registries so whenever a new tool, subagent, or skill is
    added, North automatically knows what it can do without any prompt file edits.
    """
    lines: list[str] = [
        "## Platform Capabilities & Ecosystem Overview",
        "North is an autonomous personal operating system with dynamic tools, specialized agents, and procedural skills:",
    ]

    # 1. Specialized Agents
    agent_registry = getattr(deps, "agent_registry", None)
    if agent_registry is not None:
        try:
            agents = agent_registry.all()
            if agents:
                lines.append("\n### Specialized Agents")
                for ag in sorted(agents, key=lambda a: a.name):
                    accepts = ", ".join(ag.config.accepts[:5]) if ag.config.accepts else ag.domain
                    lines.append(f"- **{ag.name}** (domain: `{ag.domain}`): accepts [{accepts}]")
        except Exception:
            pass

    # 2. All Available Tools
    tool_registry = getattr(deps, "tool_registry", None)
    if tool_registry is not None:
        try:
            tools = tool_registry.all_tools()
            if tools:
                lines.append("\n### Available Tools & Integrations")
                for t in sorted(tools, key=lambda x: x.name):
                    summary = t.description.split(". ")[0].strip()
                    lines.append(f"- **{t.name}**: {summary}")
        except Exception:
            pass

    # 3. Procedural Skills
    skill_registry = getattr(deps, "skill_registry", None)
    if skill_registry is not None:
        try:
            skills = skill_registry.all()
            if skills:
                lines.append("\n### Procedural Skills")
                for s in sorted(skills, key=lambda x: x.name):
                    lines.append(f"- **{s.name}**: {s.description}")
        except Exception:
            pass

    # 4. Meta-Capabilities & Self-Extension
    lines.append("\n### Meta-Capabilities (Self-Extension)")
    lines.append("- **create_tool**: Create, update, or hot-reload custom Python tools at runtime.")
    lines.append("- **create_agent**: Create new specialized sub-agents with custom prompts and configs at runtime.")
    lines.append("- **use_skill**: Access and execute procedural skills and workflows.")

    lines.append(
        "\nWhen asked what North can do, answer accurately using the platform capabilities above. "
        "North can delegate tasks to its specialized agents, execute work directly via these tools and skills, "
        "or create new tools and agents on the fly when new capabilities are needed."
    )
    return "\n".join(lines)
