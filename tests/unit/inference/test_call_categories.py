"""Every inference call is attributed to a cockpit-purpose category."""

from __future__ import annotations

from inference.cost_tracker import CostTracker, inference_call_category
from inference.models import CompletionRequest, ToolCallRequest
from tests.conftest import MockInferenceRouter


def test_inference_call_category_separates_agent_and_internal_work() -> None:
    assert inference_call_category("researcher", "tool_completion") == "agent"
    assert inference_call_category("planner", "completion") == "planning"
    assert inference_call_category("coder:compact", "completion") == "context"
    assert inference_call_category("critic", "completion") == "review"
    assert inference_call_category("extraction_pipeline", "completion") == "memory"
    assert inference_call_category("unknown_worker", "completion") == "background"


async def test_cost_tracker_emits_content_free_call_attribution() -> None:
    recorded: list[tuple[str | None, dict]] = []

    async def sink(task_id: str | None, data: dict) -> None:
        recorded.append((task_id, data))

    tracker = CostTracker(MockInferenceRouter(), call_sink=sink)
    await tracker.complete(CompletionRequest(prompt="private planning prompt", component="planner", task_id="t1"))
    await tracker.complete_with_tools(
        ToolCallRequest(
            messages=[{"role": "user", "content": "private agent prompt"}],
            tools=[],
            component="researcher",
            task_id="t1",
            run_id="r1",
        )
    )

    assert [item[1]["category"] for item in recorded] == ["planning", "agent"]
    assert recorded[1][1]["run_id"] == "r1"
    assert "private" not in str(recorded)
