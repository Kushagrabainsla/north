"""Dynamic, cache-friendly platform capability manifest.

Tool names provide lean global awareness; task-specific descriptions and schemas
arrive through retrieval. Agent and skill metadata remains dynamic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.models import AgentDependencies


def build_platform_capabilities_summary(deps: AgentDependencies) -> str:
    """Build a lean summary of the tools and flows registered in North.

    Queries runtime registries so whenever a new tool or flow is
    added, North knows it exists without carrying every tool description.
    """
    lines: list[str] = [
        "## Platform Capabilities & Ecosystem Overview",
        "North is an autonomous personal operating system with dynamic tools and procedural skills:",
    ]

    # 1. Stable, lean awareness catalog. Full descriptions and parameter schemas
    # are retrieved per task; names are enough for the model to know what exists.
    tool_registry = getattr(deps, "tool_registry", None)
    if tool_registry is not None:
        try:
            tools = tool_registry.all_tools()
            if tools:
                names = ", ".join(t.name for t in sorted(tools, key=lambda x: x.name))
                lines.append("\n### Available Tool Names")
                lines.append(names)
                lines.append("Use `find_tools` when the needed tool is not currently loaded.")
        except Exception:
            pass

    # Skills are not listed here. The task message names the ones this agent may
    # use, matching ones with their descriptions; the full catalog here was about
    # 2,700 tokens on every step, and it showed skills the agent may not use.

    # 2. Declarative Flows
    flow_registry = getattr(deps, "flow_registry", None)
    if flow_registry is not None:
        try:
            flows = flow_registry.all()
            if flows:
                lines.append("\n### Reusable Flows")
                for flow in sorted(flows, key=lambda item: item.name):
                    lines.append(f"- **{flow.name}** ({flow.status}, {len(flow.steps)} steps): {flow.description}")
        except Exception:
            pass

    # 3. Meta-Capabilities & Self-Extension
    lines.append("\n### Meta-Capabilities (Self-Extension)")
    lines.append(
        "- **create_tool**: Stage a Python tool candidate, validate and test it, then activate it with confirmation."
    )
    lines.append(
        "- **create_skill**: Stage reusable procedural guidance and an optional flow-execution contract, "
        "test selection, then activate it with confirmation."
    )
    lines.append(
        "- **create_flow**: Compose active skills into a candidate process, then validate and test it "
        "before activation."
    )
    lines.append("- **schedule_task**: Run an active flow later, once or on a routine or fixed interval.")
    lines.append("- **use_skill**: Access a reusable procedure that may call several atomic tools.")
    lines.append("- **run_flow**: Test or execute a validated declarative flow with persisted checkpoints.")

    lines.append(
        "\nWhen asked what North can do, answer accurately using the platform capabilities above. "
        "The execution hierarchy is flow to skill to tool: flows reference only skills; executable skills own "
        "their exact tools, I/O contract, approval floor, and success checks; tools provide atomic "
        "operations. North can create new tools, skills and flows when new capabilities are needed."
    )
    return "\n".join(lines)
