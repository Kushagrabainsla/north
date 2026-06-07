"""AgenticLLMAgent — ReAct-loop agent using native function calling + streaming.

Uses the OpenAI-compatible tools API instead of JSON-in-text so the model
reliably selects and invokes functions.  Text tokens from the final answer
are forwarded via SSE ``token`` events as they stream in (task 4).

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from agents.constants import (
    _CREATE_TOOL_PREVIEW_CHARS,
    _TOOL_RESULT_MIN_FIELD_CHARS,
    ENGINEERING_AGENTS,
    MAX_DELEGATION_DEPTH,
    MAX_TOOL_RESULT_CHARS,
)
from agents.context_compaction import (
    COMPACT_KEEP_RECENT_OVERFLOW,
    COMPACT_TOKENS_DEFAULT,
    COMPACT_TOKENS_HEAVY,
    HEAVY_OUTPUT_TOOLS,
    compact_history,
    compact_if_needed,
)
from agents.llm_agent import LLMAgent
from agents.models import AgentPayload
from agents.schemas import DELEGATE_TASK_SCHEMA, REQUEST_APPROVAL_SCHEMA
from approval.models import Card, CardType
from inference.exceptions import ContextTooLargeError
from inference.models import ToolCall, ToolCallRequest
from tools.base import Tool
from tools.models import ToolInput
from utils.ids import generate_id

logger = logging.getLogger(__name__)


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop via native function calling.

    Each iteration asks the model whether to call a tool or produce a final
    answer.  Text tokens from the final answer are forwarded to the SSE stream
    as they arrive so the UI can render them progressively.
    """

    def __init__(self, config: Any, deps: Any) -> None:
        super().__init__(config, deps)
        # Strong references to fire-and-forget confidence-recording tasks so
        # they are not garbage-collected before the DB write completes.
        self._background_tasks: set[asyncio.Task] = set()

    async def _record_tool_call_confidence(self, tool_name: str, success: bool) -> None:
        """Record tool execution confidence if not a special internal tool."""
        if tool_name not in ("request_approval", "delegate_task"):
            t = asyncio.create_task(
                self._deps.confidence_tracker.record_use(self.name, tool_name, success)
            )
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            t.add_done_callback(
                lambda _t: logger.warning(
                    "Background confidence recording failed for %s/%s: %s",
                    self.name, tool_name, _t.exception(),
                )
                if not _t.cancelled() and _t.exception() is not None else None
            )

    def _append_tool_call_exchange(
        self, messages: list[dict], results: list[tuple[ToolCall, str, bool]]
    ) -> None:
        """Format and append assistant tool_calls message and the respective tool outputs."""
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.params),
                    },
                }
                for call, _, _ in results
            ],
        })
        for call, result_str, _ in results:
            messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result_str,
            })

    async def _handle_tool_calls_response(
        self,
        calls: list[ToolCall],
        payload: AgentPayload,
        tool_map: dict[str, Tool],
        messages: list[dict],
    ) -> None:
        """Execute multiple tool calls in parallel and update logging/history."""
        # Announce every call *before* execution so the UI shows live in-progress
        # state rather than a retrospective log after the tool has already finished.
        if self._deps.stream_manager and payload.task_id:
            for call in calls:
                await self._deps.stream_manager.emit(
                    payload.task_id, "tool_called", {"tool": call.name, "params": call.params}
                )

        results: list[tuple[ToolCall, str, bool]] = await asyncio.gather(
            *[self._execute_call(call, payload, tool_map) for call in calls]
        )

        for call, _, success in results:
            if self._deps.stream_manager and payload.task_id:
                await self._deps.stream_manager.emit(
                    payload.task_id, "tool_result", {"tool": call.name, "success": success}
                )
            await self._record_tool_call_confidence(call.name, success)

        self._append_tool_call_exchange(messages, results)

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        system_prompt = f"Current date/time: {now}\n\n" + self._load_system_prompt()
        user_text = self._build_task_message(payload, context, scored_tools)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        tool_map = {t.name: t for t, _ in scored_tools}
        # Agents with bash/git/patch_file produce larger tool outputs; give their
        # compaction summaries more room to preserve file paths and error messages.
        compact_tokens = (
            COMPACT_TOKENS_HEAVY if tool_map.keys() & HEAVY_OUTPUT_TOOLS
            else COMPACT_TOKENS_DEFAULT
        )
        total_cost_usd: float = 0.0
        last_tokens_in: int = 0
        last_model_used: str = ""
        _seen_tools: set[str] = set()
        tools_used: list[str] = []

        # Iteration cap is set from settings.agent_max_iterations via AgentDependencies.
        for _ in range(self._deps.agent_max_iterations):
            # Token-aware compaction: summarise old history when we approach the
            # model's context window (75% threshold). Runs before the next API
            # call so the compacted messages are what gets sent.
            if last_tokens_in > 0:
                await compact_if_needed(
                    messages,
                    tokens_in=last_tokens_in,
                    model_used=last_model_used,
                    inference_router=self._deps.inference_router,
                    component=self.name,
                    task_id=payload.task_id,
                    keep_recent=self._deps.agent_history_keep_recent,
                    max_summary_tokens=compact_tokens,
                )
            else:
                # First iteration: apply the lightweight truncation as a baseline.
                compact_history(messages, keep_recent=self._deps.agent_history_keep_recent)

            # Refresh tool_map each iteration so tools hot-loaded mid-task
            # (e.g. by create_tool) are immediately available to the LLM.
            _sync_hot_loaded_tools(self._deps, self.name, tool_map)
            tools = (
                [t.schema() for t in tool_map.values()]
                + [DELEGATE_TASK_SCHEMA, REQUEST_APPROVAL_SCHEMA]
            )

            token_cb = self._make_token_callback(payload.task_id)

            try:
                response = await self._complete_with_tools(
                    messages, tools, payload.task_id, token_cb
                )
            except ContextTooLargeError:
                compact_history(messages, keep_recent=COMPACT_KEEP_RECENT_OVERFLOW)
                try:
                    response = await self._complete_with_tools(
                        messages, tools, payload.task_id, token_cb
                    )
                except ContextTooLargeError:
                    return _final_answer(
                        "Context window exceeded — the conversation is too long to continue.",
                        "Context overflow",
                        total_cost_usd,
                        tools_used,
                    )
            total_cost_usd += response.cost_usd
            last_tokens_in = response.tokens_in
            last_model_used = response.model_used

            if response.type == "message":
                # Final answer — tokens were already streamed via token_cb.
                content = response.content or ""
                return _final_answer(content, content[:120], total_cost_usd, tools_used)

            # Tool calls branch — execute all calls in parallel.
            if not response.calls:
                break

            for call in response.calls:
                if call.name not in _seen_tools:
                    _seen_tools.add(call.name)
                    tools_used.append(call.name)
            await self._handle_tool_calls_response(response.calls, payload, tool_map, messages)

        return _final_answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
            total_cost_usd,
            tools_used,
        )

    # ------------------------------------------------------------------

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        task_id: str,
        token_callback: Callable[[str], Awaitable[None]] | None,
    ) -> Any:
        return await self._deps.inference_router.complete_with_tools(
            ToolCallRequest(
                messages=messages,
                tools=tools,
                priority=self._resolve_priority(),
                component=self.name,
                task_id=task_id,
            ),
            token_callback=token_callback,
        )

    async def _execute_call(
        self,
        call: ToolCall,
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> tuple[ToolCall, str, bool]:
        """Execute one tool call and return (call, result_json, success)."""
        params = dict(call.params)
        if call.name == "delegate_task":
            result_str = await self._delegate_task(payload, params)
            success = json.loads(result_str).get("success", False)
            return call, result_str, success
        if call.name == "request_approval":
            decision = await self._request_approval(payload, params)
            result_str = json.dumps({"decision": decision})
            success = decision.lower() not in ("reject", "rejected", "timeout_rejected")
            return call, result_str, success
        if call.name == "create_tool" and params.get("action") in ("create", "update"):
            name = params.get("name", "unknown")
            action = params.get("action", "create")
            tool_type = params.get("tool_type", "specialized")
            content = params.get("content", "").strip()
            preview = (
                (content[:_CREATE_TOOL_PREVIEW_CHARS] + "\n…")
                if len(content) > _CREATE_TOOL_PREVIEW_CHARS
                else content
            )
            msg = (
                f"Agent wants to {action} the '{name}' tool ({tool_type}).\n\n"
                + (f"```python\n{preview}\n```" if preview else "(stub — no implementation provided)")
            )
            decision = await self._request_approval(payload, {
                "message": msg,
                "options": ["Approve", "Reject"],
            })
            if decision.lower() in ("reject", "rejected", "timeout_rejected"):
                return call, json.dumps({"success": False, "error": "User rejected tool creation."}), False
        if payload.workspace:
            params["workspace"] = payload.workspace
        if payload.task_id and "task_id" not in params:
            params["task_id"] = payload.task_id
        result_str = await self._call_tool(tool_map, call.name, params)
        return call, result_str, _extract_success(result_str)

    def _build_task_message(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> str:
        """User message without the tool list (tools are passed as function defs)."""
        reliability_lines = "\n".join(
            f"- {t.name} reliability: {score:.0%}"
            for t, score in scored_tools
        )
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        system_lines = [f"- current date/time: {now}"]
        if payload.workspace:
            system_lines.append(f"- workspace: {payload.workspace}")
        system_context = "## System Context\n" + "\n".join(system_lines) + "\n\n"

        # Split context: recent conversation goes before the task so the model
        # has conversational frame before it reads the current prompt. Personal
        # facts and episodic memory go after.
        recent_conv = ""
        background = ""
        if context:
            if context.startswith("## Recent conversation"):
                # Split only at a \n\n## boundary so multi-line conversation
                # content (which may itself contain blank lines) isn't truncated.
                parts = re.split(r"\n\n(?=##)", context, maxsplit=1)
                recent_conv = parts[0] + "\n\n"
                background = parts[1] if len(parts) > 1 else ""
            else:
                background = context

        return (
            f"{system_context}"
            f"{recent_conv}"
            f"## Task\n{payload.prompt}\n\n"
            f"## Task ID\n{payload.task_id}\n\n"
            f"## Context\n{background or '(none)'}\n\n"
            f"## Tool reliability hints\n{reliability_lines or '(none)'}\n"
        )

    def _make_token_callback(
        self, task_id: str
    ) -> Callable[[str], Awaitable[None]] | None:
        if self._deps.stream_manager is None or not task_id:
            return None
        stream_mgr = self._deps.stream_manager
        _task_id = task_id

        async def _cb(token: str) -> None:
            await stream_mgr.emit(_task_id, "token", {"text": token})

        return _cb

    async def _delegate_task(
        self, payload: AgentPayload, params: dict[str, Any]
    ) -> str:
        """Run a specialist sub-agent and return its output as a tool result."""
        if payload.delegation_depth >= MAX_DELEGATION_DEPTH:
            return json.dumps({
                "success": False,
                "error": (
                    f"Delegation depth limit ({MAX_DELEGATION_DEPTH}) reached — "
                    "you cannot delegate further. Write a final summary of what was "
                    "accomplished, what was attempted, and what remains unresolved, "
                    "then return that as your answer."
                ),
            })

        agent_name = str(params.get("agent", "general"))
        if agent_name in payload.delegation_chain:
            return json.dumps({
                "success": False,
                "error": (
                    f"Delegation cycle detected — '{agent_name}' is already in the "
                    f"current chain {payload.delegation_chain}. "
                    "Summarise what you have and return your best result instead of delegating again."
                ),
            })

        registry = self._deps.agent_registry
        if registry is None:
            return json.dumps(
                {"success": False, "error": "Agent registry not available for delegation."}
            )

        task = str(params.get("task", ""))
        if not task:
            return json.dumps(
                {"success": False, "error": "delegate_task requires a non-empty 'task' parameter."}
            )

        try:
            agent = registry.get(agent_name)
        except Exception:
            if agent_name in ENGINEERING_AGENTS:
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Engineering agent '{agent_name}' not found. "
                        "Cannot fall back to general for engineering tasks. "
                        "Ensure the agent is registered and retry."
                    ),
                })
            try:
                agent = registry.get("general")
            except Exception:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Agent '{agent_name}' not found and no 'general' fallback.",
                    }
                )

        sub_payload = AgentPayload(
            task_id=payload.task_id,
            prompt=task,
            workspace=payload.workspace,
            delegation_depth=payload.delegation_depth + 1,
            delegation_chain=payload.delegation_chain + [self.name],
        )
        try:
            result = await agent.run(sub_payload)
            return json.dumps({"success": True, "output": result.output, "summary": result.summary})
        except Exception as exc:
            logger.warning(
                "Sub-agent '%s' raised in task '%s': %s", agent_name, payload.task_id, exc, exc_info=True
            )
            return json.dumps({"success": False, "error": str(exc)})

    async def _request_approval(
        self, payload: AgentPayload, params: dict[str, Any]
    ) -> str:
        store = self._deps.approval_store
        if store is None:
            raise RuntimeError(
                f"Agent '{self.name}' called request_approval but no ApprovalStore "
                "was injected into AgentDependencies. Wire it at startup."
            )

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

        # Check learned judgement rules before surfacing to the user.
        # If a rule fires with high confidence, return the auto-decision
        # immediately without creating a pending card or emitting any SSE.
        if self._deps.judgement_filter is not None:
            try:
                auto_decision, _ = await self._deps.judgement_filter.check(card)
                if auto_decision is not None:
                    logger.debug(
                        "JudgementFilter auto-%s for agent %s: %r",
                        auto_decision, self.name, message[:80],
                    )
                    return auto_decision
            except Exception:
                logger.debug("JudgementFilter check failed for agent %s — surfacing card", self.name)

        store.add(card)

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

        current = await store.wait_for_decision(card_id, timeout=self._deps.approval_timeout_seconds)
        if current is None:
            store.resolve(card_id, "rejected")
            return "timeout_rejected"
        return current.status

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
            data = result.model_dump()
            if result.success:
                data["formatted"] = tool_map[tool_name].format_output(result.data or {})
        except Exception as exc:
            logger.warning("Tool '%s' raised: %s", tool_name, exc, exc_info=True)
            return json.dumps({"success": False, "error": str(exc)})
        raw = json.dumps(data)
        # Cap the result so a single large tool response can't exhaust the
        # model's context window. Truncate *inside* the data dict so the JSON
        # returned to the model is always syntactically valid.
        if len(raw) > MAX_TOOL_RESULT_CHARS:
            omitted = len(raw) - MAX_TOOL_RESULT_CHARS
            inner = data.get("data", {})
            per_field = max(
                _TOOL_RESULT_MIN_FIELD_CHARS,
                (MAX_TOOL_RESULT_CHARS - _TOOL_RESULT_MIN_FIELD_CHARS) // max(len(inner), 1),
            )
            data["data"] = {
                k: (v[:per_field] + "…[truncated]" if isinstance(v, str) and len(v) > per_field else v)
                for k, v in inner.items()
            }
            data["_note"] = f"{omitted} chars omitted from original output."
            raw = json.dumps(data)
        return raw


def _extract_success(tool_result_str: str) -> bool:
    try:
        return bool(json.loads(tool_result_str).get("success", False))
    except (json.JSONDecodeError, AttributeError):
        return False


def _sync_hot_loaded_tools(deps: Any, agent_name: str, tool_map: dict[str, Tool]) -> None:
    """Add any tools registered in the registry since this agent started executing."""
    registry = getattr(deps, "tool_registry", None)
    if registry is None:
        return
    for tool in registry.tools_for_agent(agent_name):
        if tool.name not in tool_map:
            tool_map[tool.name] = tool


def _final_answer(
    output: str,
    summary: str,
    cost_usd: float = 0.0,
    tools_used: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "output": output,
        "summary": summary,
        "data": {},
        "requires_approval": False,
        "has_question": False,
        "question": None,
        "question_options": [],
        "cost_usd": cost_usd,
        "tools_used": tools_used or [],
    }
