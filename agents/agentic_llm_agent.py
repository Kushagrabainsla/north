"""AgenticLLMAgent — ReAct-loop agent using native function calling + streaming.

Uses the OpenAI-compatible tools API instead of JSON-in-text so the model
reliably selects and invokes functions.  Text tokens from the final answer
are forwarded via SSE ``token`` events as they stream in (task 4).

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from agents.llm_agent import LLMAgent
from agents.models import AgentPayload
from inference.models import CompletionRequest, PoolPriority, ToolCall, ToolCallRequest
from tools.base import Tool
from tools.models import ToolInput

# supports full researcher→architect→coder↔tester chains with multiple fix cycles
_MAX_DELEGATION_DEPTH = 10

# engineering agents must be found exactly — no silent fallback to general
_ENGINEERING_AGENTS: frozenset[str] = frozenset({"researcher", "architect", "coder", "tester"})
# Cap the JSON-serialised tool result injected back into the conversation.
# ~40k chars ≈ 10k tokens — generous but bounded.
_MAX_TOOL_RESULT_CHARS = 40_000

# Special tool offered to every agent so it can gate irreversible actions.
_DELEGATE_TASK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a sub-task to a specialist agent. "
            "Use when a sub-problem clearly belongs to a different domain specialist "
            "(e.g. code, finance, health). The specialist runs its own ReAct loop and "
            "returns a result. Only use when the sub-task genuinely requires domain expertise "
            "you don't have — don't delegate work you can do yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of the specialist agent "
                        "(e.g. 'researcher', 'architect', 'coder', 'tester', "
                        "'finance', 'health', 'university', 'job', 'home', 'general')."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "The full sub-task prompt for the specialist. Be specific.",
                },
            },
            "required": ["agent", "task"],
        },
    },
}

_REQUEST_APPROVAL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "request_approval",
        "description": (
            "Request explicit user approval before taking an irreversible action "
            "(send email, submit form, delete data, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Describe exactly what you plan to do and why.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Choices shown to the user (default ['Approve','Reject']).",
                },
            },
            "required": ["message"],
        },
    },
}


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop via native function calling.

    Each iteration asks the model whether to call a tool or produce a final
    answer.  Text tokens from the final answer are forwarded to the SSE stream
    as they arrive so the UI can render them progressively.
    """

    async def _record_tool_call_confidence(self, tool_name: str, success: bool) -> None:
        """Record tool execution confidence if not a special internal tool."""
        if tool_name not in ("request_approval", "delegate_task"):
            asyncio.create_task(
                self._deps.confidence_tracker.record_use(self.name, tool_name, success)
            )

    async def _emit_tool_call_events(
        self, task_id: str, tool_name: str, params: dict, success: bool
    ) -> None:
        """Emit stream events for a completed tool call."""
        if self._deps.stream_manager and task_id:
            await self._deps.stream_manager.emit(
                task_id, "tool_called", {"tool": tool_name, "params": params}
            )
            await self._deps.stream_manager.emit(
                task_id, "tool_result", {"tool": tool_name, "success": success}
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
        results: list[tuple[ToolCall, str, bool]] = await asyncio.gather(
            *[self._execute_call(call, payload, tool_map) for call in calls]
        )

        for call, _, success in results:
            await self._emit_tool_call_events(payload.task_id, call.name, call.params, success)
            await self._record_tool_call_confidence(call.name, success)

        self._append_tool_call_exchange(messages, results)

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        system_prompt = self._load_system_prompt()
        user_text = self._build_task_message(payload, context, scored_tools)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        tool_map = {t.name: t for t, _ in scored_tools}
        total_cost_usd: float = 0.0
        last_tokens_in: int = 0
        last_model_used: str = ""

        # Iteration cap is set from settings.agent_max_iterations via AgentDependencies.
        for _ in range(self._deps.agent_max_iterations):
            # Token-aware compaction: summarise old history when we approach the
            # model's context window (40% threshold). Runs before the next API
            # call so the compacted messages are what gets sent.
            if last_tokens_in > 0:
                await _compact_if_needed(
                    messages,
                    tokens_in=last_tokens_in,
                    model_used=last_model_used,
                    inference_router=self._deps.inference_router,
                    component=self.name,
                    task_id=payload.task_id,
                    keep_recent=self._deps.agent_history_keep_recent,
                )
            else:
                # First iteration: apply the lightweight truncation as a baseline.
                _compact_history(messages, keep_recent=self._deps.agent_history_keep_recent)

            # Refresh tool_map each iteration so tools hot-loaded mid-task
            # (e.g. by create_tool) are immediately available to the LLM.
            _sync_hot_loaded_tools(self._deps, self.name, tool_map)
            tools = (
                [t.schema() for t in tool_map.values()]
                + [_DELEGATE_TASK_SCHEMA, _REQUEST_APPROVAL_SCHEMA]
            )

            token_cb = self._make_token_callback(payload.task_id)

            response = await self._deps.inference_router.complete_with_tools(
                ToolCallRequest(
                    messages=messages,
                    tools=tools,
                    priority=self._resolve_priority(),
                    component=self.name,
                    task_id=payload.task_id,
                ),
                token_callback=token_cb,
            )
            total_cost_usd += response.cost_usd
            last_tokens_in = response.tokens_in
            last_model_used = response.model_used

            if response.type == "message":
                # Final answer — tokens were already streamed via token_cb.
                content = response.content or ""
                return _final_answer(content, content[:120], total_cost_usd)

            # Tool calls branch — execute all calls in parallel.
            if not response.calls:
                break

            await self._handle_tool_calls_response(response.calls, payload, tool_map, messages)

        return _final_answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
            total_cost_usd,
        )

    # ------------------------------------------------------------------

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
            preview = (content[:1500] + "\n…") if len(content) > 1500 else content
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
        if payload.workspace and "workspace" not in params:
            params["workspace"] = payload.workspace
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
        return (
            f"## Task\n{payload.prompt}\n\n"
            f"## Task ID\n{payload.task_id}\n\n"
            f"## Context\n{context or '(none)'}\n\n"
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
        if payload.delegation_depth >= _MAX_DELEGATION_DEPTH:
            return json.dumps({
                "success": False,
                "error": (
                    f"Delegation depth limit ({_MAX_DELEGATION_DEPTH}) reached. "
                    "Solve this sub-task directly instead of delegating further."
                ),
            })

        registry = self._deps.agent_registry
        if registry is None:
            return json.dumps(
                {"success": False, "error": "Agent registry not available for delegation."}
            )

        agent_name = str(params.get("agent", "general"))
        task = str(params.get("task", ""))
        if not task:
            return json.dumps(
                {"success": False, "error": "delegate_task requires a non-empty 'task' parameter."}
            )

        try:
            agent = registry.get(agent_name)
        except Exception:
            if agent_name in _ENGINEERING_AGENTS:
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
        )
        try:
            result = await agent.run(sub_payload)
            return json.dumps({"success": True, "output": result.output, "summary": result.summary})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    async def _request_approval(
        self, payload: AgentPayload, params: dict[str, Any]
    ) -> str:
        from approval.models import Card, CardType
        from utils.ids import generate_id

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

        current = await store.wait_for_decision(card_id, timeout=300.0)
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
            raw = json.dumps(result.model_dump())
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})
        # Cap the serialised result so a single large tool response can't exhaust
        # the model's context window.
        if len(raw) > _MAX_TOOL_RESULT_CHARS:
            omitted = len(raw) - _MAX_TOOL_RESULT_CHARS
            raw = raw[:_MAX_TOOL_RESULT_CHARS] + f'…[{omitted} chars truncated]"'
        return raw


def _compact_history(messages: list[dict], keep_recent: int = 4) -> None:
    """Compacts the history by truncating older tool responses to save context.

    Also truncates the arguments on the paired assistant tool_call so both
    halves of the exchange shrink together — preventing context bloat from
    large input payloads that were already executed.
    """
    tool_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return

    # Build a map from tool_call_id -> index of the assistant message that owns it.
    call_id_to_assistant: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id")
                if cid:
                    call_id_to_assistant[cid] = i

    to_compact = tool_indices[:-keep_recent]
    compacted_assistant: set[int] = set()

    for idx in to_compact:
        msg = messages[idx]
        content = msg.get("content")
        if isinstance(content, str) and len(content) > 500:
            truncated = True
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    minimal = {}
                    if "success" in data:
                        minimal["success"] = data["success"]
                    if "error" in data:
                        minimal["error"] = data["error"]
                    minimal["_note"] = "Large tool output truncated to save context window."
                    msg["content"] = json.dumps(minimal)
                    truncated = False
            except Exception:
                pass

            if truncated:
                msg["content"] = content[:300] + "... [Large tool output truncated to save context]"

        # Also compact the arguments on the paired assistant tool_call.
        call_id = msg.get("tool_call_id")
        if call_id and call_id in call_id_to_assistant:
            ast_idx = call_id_to_assistant[call_id]
            if ast_idx not in compacted_assistant:
                ast_msg = messages[ast_idx]
                for tc in ast_msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str) and len(args) > 200:
                        fn["arguments"] = "{}"  # keep structure, drop large args
                compacted_assistant.add(ast_idx)


def _extract_success(tool_result_str: str) -> bool:
    try:
        return bool(json.loads(tool_result_str).get("success", False))
    except (json.JSONDecodeError, AttributeError):
        return False


_COMPACTION_THRESHOLD = 0.40  # compact when tokens_in hits this fraction of context window


def _context_window_for(model: str) -> int:
    """Return the published context-window size (tokens) for a model identifier."""
    m = model.lower()
    if "gemini-2" in m or "gemini-1.5" in m:
        return 1_000_000
    if "gemini" in m:
        return 128_000
    if "claude" in m:
        return 200_000
    if "gpt-4o" in m or "gpt-4-turbo" in m or "gpt-4.1" in m:
        return 128_000
    if "llama" in m or "qwen" in m or "mistral" in m or "deepseek" in m:
        return 128_000
    return 128_000  # conservative default


def _exchange_boundaries(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (start, end_inclusive) index pairs for each tool-call exchange.

    An exchange = one assistant message that has tool_calls + all the tool
    result messages that immediately follow it.
    """
    exchanges: list[tuple[int, int]] = []
    i = 2  # skip [0]=system, [1]=user-task
    while i < len(messages):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            start = i
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                j += 1
            exchanges.append((start, j - 1))
            i = j
        else:
            i += 1
    return exchanges


def _render_exchange_for_summary(messages: list[dict]) -> str:
    """Format a slice of the message list into a short readable string for summarisation."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                    args_str = json.dumps(args)[:200]
                except Exception:
                    args_str = str(fn.get("arguments", ""))[:200]
                lines.append(f"→ tool call: {name}({args_str})")
        elif role == "tool":
            content = msg.get("content", "")
            try:
                data = json.loads(content) if isinstance(content, str) else {}
                success = data.get("success", True)
                result_parts = ["ok" if success else "failed"]
                for k, v in data.items():
                    if k not in ("success", "_note"):
                        result_parts.append(f"{k}={str(v)[:80]}")
                lines.append(f"  ← result: {', '.join(result_parts[:5])}")
            except Exception:
                lines.append(f"  ← result: {str(content)[:200]}")
        elif role == "user":
            lines.append(f"[user context: {str(msg.get('content', ''))[:200]}]")
    return "\n".join(lines)


async def _compact_if_needed(
    messages: list[dict],
    *,
    tokens_in: int,
    model_used: str,
    inference_router: Any,
    component: str,
    task_id: str | None,
    keep_recent: int,
) -> None:
    """LLM-summarise old exchanges when token usage exceeds 40% of the context window.

    Keeps [0] system, [1] user-task, and the last `keep_recent` tool exchanges
    verbatim. Everything in between is replaced with a single summarised block.
    Falls back to truncation-only if the summarisation call fails.
    """
    context_window = _context_window_for(model_used)
    if tokens_in < context_window * _COMPACTION_THRESHOLD:
        # Still well within budget — just do the lightweight truncation.
        _compact_history(messages, keep_recent=keep_recent)
        return

    exchanges = _exchange_boundaries(messages)
    if len(exchanges) <= keep_recent:
        # Not enough history to make summarisation worthwhile.
        _compact_history(messages, keep_recent=keep_recent)
        return

    # Split: summarise everything before the last `keep_recent` exchanges.
    first_kept = exchanges[-keep_recent][0]
    to_summarise = messages[2:first_kept]  # exclude system(0) + user-task(1)

    if not to_summarise:
        return

    history_text = _render_exchange_for_summary(to_summarise)
    prompt = (
        "You are summarising intermediate steps of an ongoing AI agent task.\n"
        "Condense the following tool calls and results into a concise bullet-point summary.\n"
        "Preserve: what was accomplished, key facts discovered, and any important data values.\n"
        "Omit: raw file contents, verbose outputs, redundant retries.\n"
        "Max 350 words.\n\n"
        f"<history>\n{history_text}\n</history>"
    )

    summary: str
    try:
        resp = await inference_router.complete(
            CompletionRequest(
                prompt=prompt,
                priority=PoolPriority.LOW,
                component=f"{component}:compact",
                task_id=task_id,
                max_tokens=512,
            )
        )
        summary = resp.text.strip()
    except Exception:
        # Summarisation failed — fall back to the character-truncation path.
        _compact_history(messages, keep_recent=keep_recent)
        return

    # Replace the old exchanges with a pair of messages that preserve the
    # user/assistant turn structure the API requires.
    messages[2:first_kept] = [
        {
            "role": "user",
            "content": f"## Earlier context (auto-compacted)\n{summary}",
        },
        {
            "role": "assistant",
            "content": "Understood — I have the compacted context.",
        },
    ]


def _sync_hot_loaded_tools(deps: Any, agent_name: str, tool_map: dict[str, Tool]) -> None:
    """Add any tools registered in the registry since this agent started executing."""
    registry = getattr(deps, "tool_registry", None)
    if registry is None:
        return
    for tool in registry.tools_for_agent(agent_name):
        if tool.name not in tool_map:
            tool_map[tool.name] = tool


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
