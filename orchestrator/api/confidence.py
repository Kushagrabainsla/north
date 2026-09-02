"""Per-agent tool confidence scores."""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.api.deps import _get_agent_registry, _get_confidence_tracker, router


class ToolConfidenceOut(BaseModel):
    agent: str
    tool: str
    confidence: float


@router.get("/tools/confidence", response_model=list[ToolConfidenceOut])
async def tool_confidence(agent: str | None = None) -> list[ToolConfidenceOut]:
    """Tool confidence scores, optionally filtered by agent."""
    tracker = _get_confidence_tracker()
    registry = _get_agent_registry()

    agents_list = [agent] if agent is not None else registry.names()
    results: list[ToolConfidenceOut] = []

    for agent_name in agents_list:
        try:
            agent_instance = registry.get(agent_name)
            allowed_tools = agent_instance.deps.tool_registry.tools_for_agent(agent_name)
        except Exception:
            continue

        agent_results = []
        for tool in allowed_tools:
            confidence_score = await tracker.get_score(agent_name, tool.name)
            agent_results.append(
                ToolConfidenceOut(
                    agent=agent_name,
                    tool=tool.name,
                    confidence=confidence_score,
                )
            )

        agent_results.sort(key=lambda item: (-item.confidence, item.tool))
        results.extend(agent_results)

    return results


