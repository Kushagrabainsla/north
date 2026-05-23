"""AgenticLLMAgent — ReAct-loop agent with real tool execution.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import json
import re
from typing import Any

from inference.models import CompletionRequest
from tools.base import Tool
from tools.models import ToolInput

from agents.llm_agent import LLMAgent
from agents.models import AgentPayload

_MAX_ITERATIONS = 12

# Appended to every system prompt. Single source of truth for response format.
REACT_FORMAT_SUFFIX = """
---

## Response format

Every response must be exactly one raw JSON object — no markdown fences, no text outside the JSON.

To call a tool:
{"tool": "<tool_name>", "params": {"key": "value"}}

To give a final answer:
{"output": "<markdown response to the user>", "summary": "<one-line summary>", "requires_approval": false, "has_question": false, "question": null, "question_options": [], "data": {}}

Rules:
- Use `"tool"` when you need to call a tool. Do not include `"output"` in the same object.
- Use `"output"` when you have a complete answer. Do not include `"tool"` in the same object.
- After each tool result, decide whether to call another tool or give the final answer.
- Set `requires_approval` to true before taking any irreversible action on the user's behalf.
- Set `has_question` to true if you need clarification before proceeding.
"""


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop: think → tool call → observe → repeat until final answer."""

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        tools: list[Tool],
    ) -> dict[str, Any]:
        system_prompt = self._load_system_prompt() + REACT_FORMAT_SUFFIX
        user_message = self._build_user_message(payload, context, tools)
        tool_map = {t.name: t for t in tools}

        conversation = f"## User\n{user_message}\n\n"

        for _ in range(_MAX_ITERATIONS):
            full_prompt = f"{system_prompt}\n\n---\n\n{conversation}## Assistant\n"

            response = await self._deps.inference_router.complete(
                CompletionRequest(
                    prompt=full_prompt,
                    priority=self._resolve_priority(),
                    component=self.name,
                    task_id=payload.task_id,
                )
            )

            raw = response.text.strip()
            parsed = _parse_json(raw)

            if parsed is None:
                # Model returned plain text instead of JSON — treat as final answer
                return _final_answer(raw, raw[:120])

            if "tool" in parsed:
                tool_name = parsed["tool"]
                params: dict[str, Any] = dict(parsed.get("params") or {})

                if payload.workspace and "workspace" not in params:
                    params["workspace"] = payload.workspace

                conversation += f"{raw}\n\n"

                if self._deps.stream_manager and payload.task_id:
                    await self._deps.stream_manager.emit(
                        payload.task_id, "tool_called", {"tool": tool_name, "params": params}
                    )

                tool_result_str = await self._call_tool(tool_map, tool_name, params)

                if self._deps.stream_manager and payload.task_id:
                    success = '"success": true' in tool_result_str
                    await self._deps.stream_manager.emit(
                        payload.task_id, "tool_result", {"tool": tool_name, "success": success}
                    )

                conversation += f"## Tool Result ({tool_name})\n{tool_result_str}\n\n"
                continue

            if "output" in parsed:
                return {
                    "output": parsed["output"],
                    "summary": parsed.get("summary", parsed["output"][:120]),
                    "data": parsed.get("data", {}),
                    "requires_approval": bool(parsed.get("requires_approval", False)),
                    "has_question": bool(parsed.get("has_question", False)),
                    "question": parsed.get("question"),
                    "question_options": parsed.get("question_options", []),
                }

            # Parsed JSON but neither "tool" nor "output" — treat raw as final answer
            return _final_answer(raw, raw[:120])

        return _final_answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
        )

    async def _call_tool(
        self,
        tool_map: dict[str, Tool],
        tool_name: str,
        params: dict[str, Any],
    ) -> str:
        if tool_name not in tool_map:
            return json.dumps({
                "success": False,
                "error": f"Tool '{tool_name}' not found. Available: {sorted(tool_map)}",
            })
        try:
            result = await tool_map[tool_name].run(ToolInput(params=params))
            return json.dumps(result.model_dump())
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})


def _parse_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _final_answer(output: str, summary: str) -> dict[str, Any]:
    return {
        "output": output,
        "summary": summary,
        "data": {},
        "requires_approval": False,
        "has_question": False,
        "question": None,
        "question_options": [],
    }
