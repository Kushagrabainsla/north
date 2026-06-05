"""Unit tests for ExecutionPlanner / Router.

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from orchestrator.router import ExecutionPlanner
from inference import CompletionResponse


@pytest.mark.asyncio
async def test_execution_planner_workspace_context_in_prompt() -> None:
    """ExecutionPlanner must inject the workspace and absolute path instruction

    into the planner prompt if a workspace path is configured.
    """
    mock_agent = MagicMock()
    mock_agent.name = "general"
    mock_agent.domain = "general"
    mock_agent.config.accepts = "text"

    mock_agent_registry = MagicMock()
    mock_agent_registry.all.return_value = [mock_agent]
    mock_agent_registry.names.return_value = ["general"]

    mock_inference = MagicMock()
    # Mock return value of complete
    response_data = {
        "confidence": 0.9,
        "is_consequential": False,
        "domain": "general",
        "reasoning": "test",
        "mode": "single_agent",
        "agents": ["general"],
    }
    mock_response = MagicMock(spec=CompletionResponse)
    mock_response.text = '{"confidence": 0.9, "is_consequential": false, "domain": "general", "reasoning": "test", "mode": "single_agent", "agents": ["general"]}'
    mock_inference.complete = AsyncMock(return_value=mock_response)

    planner = ExecutionPlanner(
        agent_registry=mock_agent_registry,
        inference_router=mock_inference,
        tool_registry=None,
        workspace="/path/to/my/workspace",
    )

    classification, plan = await planner.plan_all(
        prompt="List directory contents",
        task_id="t1",
    )

    # Verify that mock_inference.complete was called
    mock_inference.complete.assert_called_once()
    call_arg = mock_inference.complete.call_args[0][0]
    
    # Verify the workspace instruction was injected in the prompt
    assert "=== System Context ===" in call_arg.prompt
    assert "- workspace (default cwd for shell/file tools): /path/to/my/workspace" in call_arg.prompt
    assert "always prefer absolute paths" in call_arg.prompt
