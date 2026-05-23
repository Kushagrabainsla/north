"""AgenticLLMAgent — ReAct-loop agent with real tool execution.

Subclasses LLMAgent and overrides `_execute()` to loop: think → tool call →
observe result → think again, until the model emits a final_answer or the
iteration cap is reached.

Existing single-call agents (job, finance, health, university) extend LLMAgent
directly and are completely unaffected by this class.

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

# Appended to every system prompt loaded by AgenticLLMAgent.
REACT_FORMAT_SUFFIX = """
---

## Response format

At every step respond with exactly one raw JSON object — no markdown fences, no text outside the JSON.

To call a tool:
{"action": "tool_call", "tool": "<tool_name>", "params": {"key": "value"}}

When you have the final answer:
{"action": "final_answer", "output": "<markdown response>", "summary": "<one-line summary>", "data": {}, "requires_approval": false, "has_question": false, "question": null, "question_options": []}
"""


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop instead of a single LLM call.

    Each iteration: build the full conversation prompt → call LLM →
    parse action → if tool_call execute tool and append result → repeat.
    Stops on final_answer or after _MAX_ITERATIONS.
    """

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        tools: list[Tool],
    ) -> dict[str, Any]:
        system_prompt = self._load_system_prompt() + REACT_FORMAT_SUFFIX
        user_message = self._build_user_message(payload, context, tools)
        tool_map = {t.name: t for t in tools}

        # Running transcript appended to each iteration's prompt
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
                return _text_result(raw)

            action = parsed.get("action")

            if action == "final_answer":
                return {
                    "output": parsed.get("output", ""),
                    "summary": parsed.get("summary", ""),
                    "data": parsed.get("data", {}),
                    "requires_approval": bool(parsed.get("requires_approval", False)),
                    "has_question": bool(parsed.get("has_question", False)),
                    "question": parsed.get("question"),
                    "question_options": parsed.get("question_options", []),
                }

            if action == "tool_call":
                tool_name = parsed.get("tool", "")
                params: dict[str, Any] = dict(parsed.get("params") or {})

                # Inject workspace automatically so agents don't have to include it
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

            # Unknown action — treat the raw text as the final answer
            return _text_result(raw)

        return {
            "output": "Reached the maximum number of reasoning steps without a final answer.",
            "summary": "Iteration limit reached",
            "data": {},
            "requires_approval": False,
            "has_question": False,
            "question": None,
            "question_options": [],
        }

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


def _text_result(text: str) -> dict[str, Any]:
    return {
        "output": text,
        "summary": text[:120],
        "data": {},
        "requires_approval": False,
        "has_question": False,
        "question": None,
        "question_options": [],
    }
