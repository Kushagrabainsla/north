"""AgenticLLMAgent — ReAct-loop agent with real tool execution.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import asyncio
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

To request user approval before an irreversible action (send email, submit form, delete data, etc.):
{"tool": "request_approval", "params": {"message": "<describe exactly what you want to do and why>", "options": ["Approve", "Reject"]}}
The tool returns {"decision": "Approve"|"Reject"|"<chosen option>"}. Only proceed if approved.

To give a final answer:
{"output": "<markdown response to the user>", "summary": "<one-line summary>", "has_question": false, "question": null, "question_options": [], "data": {}}

Rules:
- Use `"tool"` when you need to call a tool or request approval.
- Use `"output"` when you have a complete answer. Do not include `"tool"` in the same object.
- After each tool result, decide whether to call another tool or give the final answer.
- Set `has_question` to true and populate `question` if you need clarification before proceeding.
"""


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop: think → tool call → observe → repeat until final answer."""

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        system_prompt = self._load_system_prompt() + REACT_FORMAT_SUFFIX
        user_message = self._build_user_message(payload, context, scored_tools)
        tool_map = {t.name: t for t, _ in scored_tools}

        conversation = f"## User\n{user_message}\n\n"
        total_cost_usd: float = 0.0

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
            total_cost_usd += response.cost_usd

            raw = response.text.strip()
            parsed = _parse_json(raw)

            if parsed is None:
                # Model returned plain text instead of JSON — treat as final answer
                return _final_answer(raw, raw[:120], total_cost_usd)

            if "tool" in parsed:
                tool_name = parsed["tool"]
                params: dict[str, Any] = dict(parsed.get("params") or {})

                conversation += f"{raw}\n\n"

                if tool_name == "request_approval":
                    if self._deps.stream_manager and payload.task_id:
                        await self._deps.stream_manager.emit(
                            payload.task_id, "tool_called", {"tool": "request_approval", "params": params}
                        )
                    decision = await self._request_approval(payload, params)
                    tool_result_str = json.dumps({"decision": decision})
                    if self._deps.stream_manager and payload.task_id:
                        await self._deps.stream_manager.emit(
                            payload.task_id, "tool_result", {"tool": "request_approval", "success": True}
                        )
                    conversation += f"## Tool Result (request_approval)\n{tool_result_str}\n\n"
                    continue

                if payload.workspace and "workspace" not in params:
                    params["workspace"] = payload.workspace

                if self._deps.stream_manager and payload.task_id:
                    await self._deps.stream_manager.emit(
                        payload.task_id, "tool_called", {"tool": tool_name, "params": params}
                    )

                tool_result_str = await self._call_tool(tool_map, tool_name, params)

                was_helpful = _extract_success(tool_result_str)
                asyncio.create_task(
                    self._deps.confidence_tracker.record_use(self.name, tool_name, was_helpful)
                )

                if self._deps.stream_manager and payload.task_id:
                    await self._deps.stream_manager.emit(
                        payload.task_id, "tool_result", {"tool": tool_name, "success": was_helpful}
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
                    "cost_usd": total_cost_usd,
                }

            # Parsed JSON but neither "tool" nor "output" — treat raw as final answer
            return _final_answer(raw, raw[:120], total_cost_usd)

        return _final_answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
            total_cost_usd,
        )

    async def _request_approval(
        self, payload: AgentPayload, params: dict[str, Any]
    ) -> str:
        """Create an approval card, emit SSE, and block until the user decides."""
        from approval.models import Card, CardType
        from approval.store import approval_store
        from utils.ids import generate_id

        message = str(params.get("message", "Action requires your approval."))
        options = list(params.get("options", ["Approve", "Reject"]))
        card_id = generate_id()

        card = Card(
            id=card_id,
            type=CardType.APPROVAL,
            task_id=payload.task_id,
            agent=self.name,
            title=f"{self.name.title()} — Approval Required",
            message=message,
            options=options,
        )
        approval_store.add(card)

        if self._deps.stream_manager and payload.task_id:
            await self._deps.stream_manager.emit(
                payload.task_id,
                "approval_required",
                {
                    "card_id": card_id,
                    "task_id": payload.task_id,
                    "agent": self.name,
                    "title": card.title,
                    "message": message,
                    "options": options,
                },
            )

        # Poll up to 5 minutes for the user's decision.
        for _ in range(300):
            await asyncio.sleep(1)
            current = approval_store.get(card_id)
            if current and current.status != "pending":
                return current.status

        approval_store.resolve(card_id, "rejected")
        return "timeout_rejected"

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


def _extract_success(tool_result_str: str) -> bool:
    """Return the `success` flag from a tool-result JSON string.

    Falls back to False on any parse failure so a bad result always records
    as unhelpful rather than inflating confidence scores.
    """
    try:
        return bool(json.loads(tool_result_str).get("success", False))
    except (json.JSONDecodeError, AttributeError):
        return False


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


def _final_answer(output: str, summary: str, cost_usd: float = 0.0) -> dict[str, Any]:
    return {
        "output": output,
        "summary": summary,
        "data": {},
        "requires_approval": False,
        "has_question": False,
        "question": None,
        "question_options": [],
        "cost_usd": cost_usd,
    }
