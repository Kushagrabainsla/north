"""No tool result enters the conversation larger than MAX_TOOL_RESULT_CHARS.

The cap shortened only the fields inside a result's `data`. The agent loop also
adds `formatted`, the tool's readable copy (the whole file, line-numbered, for
read_file), beside it, and that copy was never shortened. In the issue #76 test
one read of a 256 KB file took the agent's input from 29,135 to 153,511 tokens a
call, resent on every later step, and a 30 MB file ended its task with "the
conversation is too long" on a short conversation. A sub-agent's answer to
delegate_task never went through the cap at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.agentic_llm_agent import _cap_tool_result
from agents.constants import MAX_TOOL_RESULT_CHARS
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from inference.models import ToolCall
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
from tools.output_spill import spill_store
from tools.registry import ToolRegistry
from tools.universal.read_file import ReadFileTool

AGENTS_DIR = Path(__file__).parents[3] / "agents"
NEEDLE = "record 1500: the one the user asked about"


def _agent(tmp_path: Path):
    from agents.general.agent import GeneralAgent

    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    return GeneralAgent(AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml"), deps)


async def _enter(agent, tmp_path: Path, call: ToolCall, tools: dict[str, Tool]) -> dict[str, Any]:
    """Run *call* the way the loop does and return the result as it entered the conversation."""
    messages: list[dict] = []
    payload = AgentPayload(task_id="t1", prompt="read it", workspace=str(tmp_path), granted_workspace=str(tmp_path))
    await agent._handle_tool_calls_response([call], payload, tools, messages)
    (entered,) = [message for message in messages if message["role"] == "tool"]
    assert len(entered["content"]) <= MAX_TOOL_RESULT_CHARS
    return json.loads(entered["content"])


async def test_reading_a_large_file_enters_the_conversation_within_the_cap(tmp_path: Path) -> None:
    lines = [f"record {i}: status=ok region=eu-west amount={i * 7} note=routine entry" for i in range(3_000)]
    lines[1500] = NEEDLE
    (tmp_path / "medium.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert (tmp_path / "medium.txt").stat().st_size > 200_000

    result = await _enter(
        _agent(tmp_path),
        tmp_path,
        ToolCall(name="read_file", call_id="c1", params={"path": str(tmp_path / "medium.txt")}),
        {"read_file": ReadFileTool()},
    )

    assert result["success"] is True
    assert "read_tool_output" in result["_note"]
    # What was left out is kept, and the agent is told how to reach it.
    assert spill_store().find(result["_handle"], NEEDLE)


class _Failing(Tool):
    name = "flaky"
    description = "Fails with a very long error."
    parameters: dict = {"type": "object", "properties": {}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=False, error="Traceback: " + "frame\n" * 20_000, failure_kind="error")


async def test_a_huge_error_enters_within_the_cap_and_still_reads_as_a_failure(tmp_path: Path) -> None:
    result = await _enter(
        _agent(tmp_path), tmp_path, ToolCall(name="flaky", call_id="c2", params={}), {"flaky": _Failing()}
    )

    assert result["success"] is False
    assert result["failure_kind"] == "error"


async def test_a_sub_agents_long_answer_enters_within_the_cap(tmp_path: Path, monkeypatch) -> None:
    agent = _agent(tmp_path)

    async def long_answer(payload, params) -> str:
        return json.dumps({"success": True, "run_id": "r2", "output": "finding\n" * 30_000, "summary": "done"})

    monkeypatch.setattr(agent, "_delegate_task", long_answer)

    result = await _enter(agent, tmp_path, ToolCall(name="delegate_task", call_id="c3", params={"task": "x"}), {})

    assert result["success"] is True
    assert result["_handle"]


def test_status_fields_survive_when_everything_else_is_reduced() -> None:
    data = {
        "success": False,
        "failure_kind": "refused",
        "error": "e" * 60_000,
        "formatted": "f" * 60_000,
        "data": {"rows": [{"i": i, "pad": "z" * 100} for i in range(5_000)]},
    }

    raw = _cap_tool_result(data, "search_files")

    assert len(raw) <= MAX_TOOL_RESULT_CHARS
    result = json.loads(raw)
    assert (result["success"], result["failure_kind"]) == (False, "refused")
    assert spill_store().read(result["_handle"]) is not None
