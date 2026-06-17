"""AgenticLLMAgent - ReAct-loop agent using native function calling + streaming.

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
from typing import Any

from agents.constants import (
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
from agents.reasoning import ReasoningStreamSplitter, strip_reasoning
from agents.schemas import ASK_USER_SCHEMA, DELEGATE_TASK_SCHEMA, REQUEST_APPROVAL_SCHEMA
from agents.user_interaction import APPROVAL_DEFAULT_OPTIONS, CardEvent, surface_card
from agents.workspace_lock import workspace_lock
from approval.models import ApprovalDecision, Card, CardType
from inference.exceptions import ContextTooLargeError
from inference.models import ToolCall, ToolCallRequest
from tools._path import handoff_dir_for
from tools.base import Tool
from tools.models import ToolInput
from utils.time import localnow

logger = logging.getLogger(__name__)

# Returned to the model when ask_user times out with no answer - steers it to be
# explicit about the gap rather than silently fabricating the missing detail.
_ASK_USER_NO_ANSWER = (
    "No answer received in time. Do not fabricate the missing detail - state the "
    "specific assumption you are forced to make, or stop and summarise exactly what "
    "you still need from the user."
)

# Agent-loop built-ins, not registry tools - excluded from tool-reliability tracking.
_INTERNAL_TOOLS = frozenset({"request_approval", "delegate_task", "ask_user"})


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
        if tool_name not in _INTERNAL_TOOLS:
            t = asyncio.create_task(self._deps.confidence_tracker.record_use(self.name, tool_name, success))
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            t.add_done_callback(
                lambda _t: (
                    logger.warning(
                        "Background confidence recording failed for %s/%s: %s",
                        self.name,
                        tool_name,
                        _t.exception(),
                    )
                    if not _t.cancelled() and _t.exception() is not None
                    else None
                )
            )

    def _append_tool_call_exchange(self, messages: list[dict], results: list[tuple[ToolCall, str, bool]]) -> None:
        """Format and append assistant tool_calls message and the respective tool outputs."""
        messages.append(
            {
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
            }
        )
        for call, result_str, _ in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": result_str,
                }
            )

    async def _handle_tool_calls_response(
        self,
        calls: list[ToolCall],
        payload: AgentPayload,
        tool_map: dict[str, Tool],
        messages: list[dict],
    ) -> list[tuple[str, bool]]:
        """Execute the requested tool calls and update logging/history.

        Read-only calls run concurrently; mutating calls run sequentially so two
        edits to the same file cannot race. Results are returned in call order.
        Returns ``(tool_name, success)`` per call as evidence for output
        verification.
        """
        # Announce every call *before* execution so the UI shows live in-progress
        # state rather than a retrospective log after the tool has already finished.
        if self._deps.stream_manager and payload.task_id:
            for call in calls:
                await self._deps.stream_manager.emit(
                    payload.task_id, "tool_called", {"tool": call.name, "params": call.params}
                )

        results = await self._execute_calls_ordered(calls, payload, tool_map)

        for call, result_str, success in results:
            if self._deps.stream_manager and payload.task_id:
                event_data: dict[str, Any] = {"tool": call.name, "success": success}
                try:
                    parsed = json.loads(result_str)
                    if formatted := parsed.get("formatted"):
                        event_data["formatted"] = formatted
                    if not success and (err := parsed.get("error")):
                        event_data["error"] = err
                except (json.JSONDecodeError, AttributeError):
                    pass
                await self._deps.stream_manager.emit(payload.task_id, "tool_result", event_data)
            await self._record_tool_call_confidence(call.name, success)

        self._append_tool_call_exchange(messages, results)
        return [(call.name, success) for call, _, success in results]

    async def _execute_calls_ordered(
        self,
        calls: list[ToolCall],
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> list[tuple[ToolCall, str, bool]]:
        """Run read-only calls concurrently and mutating calls sequentially.

        Mutating tools (file writes, shell, git) are serialized under the
        per-WORKSPACE lock - not per agent instance - so a delegated coder and
        tester working in the same tree cannot interleave mutations. Every call
        is wrapped so a raised exception becomes a failed tool result rather
        than cancelling its siblings (CODING_STYLE §10.5). Results preserve the
        original call order.
        """
        results: dict[int, tuple[ToolCall, str, bool]] = {}

        concurrent = [(i, c) for i, c in enumerate(calls) if not self._is_mutating_call(c, tool_map)]
        if concurrent:
            gathered = await asyncio.gather(
                *[self._safe_execute_call(c, payload, tool_map) for _, c in concurrent],
                return_exceptions=True,
            )
            for (index, call), outcome in zip(concurrent, gathered, strict=True):
                results[index] = outcome if not isinstance(outcome, BaseException) else _failed_call(call, outcome)

        for index, call in enumerate(calls):
            if index not in results:
                if call.name == "delegate_task":
                    # Never hold the workspace lock across delegation - the
                    # sub-agent acquires it for its own mutations and would
                    # deadlock against its parent.
                    results[index] = await self._safe_execute_call(call, payload, tool_map)
                else:
                    async with workspace_lock(payload.workspace):
                        results[index] = await self._safe_execute_call(call, payload, tool_map)

        return [results[index] for index in range(len(calls))]

    async def _safe_execute_call(
        self,
        call: ToolCall,
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> tuple[ToolCall, str, bool]:
        """Execute one call, turning any unexpected exception into a failed result."""
        try:
            return await self._execute_call(call, payload, tool_map)
        except Exception as exc:
            logger.warning("Tool call '%s' raised: %s", call.name, exc, exc_info=True)
            return _failed_call(call, exc)

    def _is_mutating_call(self, call: ToolCall, tool_map: dict[str, Tool]) -> bool:
        if call.name == "delegate_task":
            return True  # a sub-agent may mutate shared files or state
        tool = tool_map.get(call.name)
        return bool(tool and tool.is_mutating)

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        messages, tool_map, compact_tokens = self._init_conversation(payload, context, scored_tools)
        total_cost_usd: float = 0.0
        total_tokens_in: int = 0
        total_tokens_out: int = 0
        last_tokens_in: int = 0
        last_model_used: str = ""
        emitted_model: str = ""
        _seen_tools: set[str] = set()
        tools_used: list[str] = []
        _seen_success: set[str] = set()
        successful_tools: list[str] = []

        # Iteration cap is set from settings.agent_max_iterations via AgentDependencies.
        for _ in range(self._deps.agent_max_iterations):
            await self._compact_for_next_call(
                messages, last_tokens_in, last_model_used, compact_tokens, payload.task_id
            )

            # Refresh tool_map each iteration so tools hot-loaded mid-task
            # (e.g. by create_tool) are immediately available to the LLM.
            _sync_hot_loaded_tools(self._deps, self.name, tool_map)
            tools = [t.schema() for t in tool_map.values()] + [
                DELEGATE_TASK_SCHEMA,
                REQUEST_APPROVAL_SCHEMA,
                ASK_USER_SCHEMA,
            ]

            token_cb = self._make_token_callback(payload.task_id)

            try:
                response = await self._complete_with_tools(messages, tools, payload.task_id, token_cb)
            except ContextTooLargeError:
                compact_history(messages, keep_recent=COMPACT_KEEP_RECENT_OVERFLOW)
                # Discard whatever the failed attempt streamed before re-streaming
                # so the splitter starts clean and UIs drop the partial output.
                if token_cb is not None:
                    await token_cb.reset()
                try:
                    response = await self._complete_with_tools(messages, tools, payload.task_id, token_cb)
                except ContextTooLargeError:
                    return _final_answer(
                        "Context window exceeded - the conversation is too long to continue.",
                        "Context overflow",
                        total_cost_usd,
                        tools_used,
                        successful_tools,
                        total_tokens_in,
                        total_tokens_out,
                    )
            # Stream finished - release any reasoning/answer fragment the splitter
            # withheld in case it began a tag that never completed.
            if token_cb is not None:
                await token_cb.flush()
            total_cost_usd += response.cost_usd
            total_tokens_in += response.tokens_in
            total_tokens_out += response.tokens_out
            last_tokens_in = response.tokens_in
            last_model_used = response.model_used
            emitted_model = await self._maybe_emit_model(response, emitted_model, payload.task_id)

            if response.type == "message":
                # Final answer - answer tokens were already streamed via token_cb;
                # strip the model's private reasoning from the stored copy so it
                # matches the streamed view and never feeds extraction/verification.
                content = strip_reasoning(response.content or "")
                return _final_answer(
                    content, content[:120], total_cost_usd, tools_used, successful_tools,
                    total_tokens_in, total_tokens_out,
                )

            # Tool calls branch - execute the requested calls.
            if not response.calls:
                return _final_answer(
                    strip_reasoning(response.content or "") or "The model returned no tool calls and no message.",
                    "No actionable response",
                    total_cost_usd,
                    tools_used,
                    successful_tools,
                    total_tokens_in,
                    total_tokens_out,
                )

            for call in response.calls:
                if call.name not in _seen_tools:
                    _seen_tools.add(call.name)
                    tools_used.append(call.name)
            evidence = await self._handle_tool_calls_response(response.calls, payload, tool_map, messages)
            for name, success in evidence:
                if success and name not in _seen_success:
                    _seen_success.add(name)
                    successful_tools.append(name)

        return _final_answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
            total_cost_usd,
            tools_used,
            successful_tools,
            total_tokens_in,
            total_tokens_out,
        )

    def _init_conversation(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> tuple[list[dict], dict[str, Tool], int]:
        """Build the initial system+user messages, tool map, and compaction budget."""
        now = localnow().strftime("%Y-%m-%d %H:%M %Z")
        system_prompt = f"Current date/time: {now}\n\n" + self._load_system_prompt()
        user_text = self._build_task_message(payload, context, scored_tools)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        tool_map = {t.name: t for t, _ in scored_tools}
        # Agents with bash/git/patch_file produce larger tool outputs; give their
        # compaction summaries more room to preserve file paths and error messages.
        compact_tokens = COMPACT_TOKENS_HEAVY if tool_map.keys() & HEAVY_OUTPUT_TOOLS else COMPACT_TOKENS_DEFAULT
        return messages, tool_map, compact_tokens

    async def _compact_for_next_call(
        self,
        messages: list[dict],
        last_tokens_in: int,
        last_model_used: str,
        compact_tokens: int,
        task_id: str,
    ) -> None:
        """Compact conversation history before the next API call.

        Token-aware: summarise old history when we approach the model's context
        window (75% threshold). On the first iteration (no token count yet) apply
        lightweight truncation as a baseline instead.
        """
        if last_tokens_in <= 0:
            compact_history(messages, keep_recent=self._deps.agent_history_keep_recent)
            return
        msgs_before = len(messages)
        await compact_if_needed(
            messages,
            tokens_in=last_tokens_in,
            model_used=last_model_used,
            inference_router=self._deps.inference_router,
            component=self.name,
            task_id=task_id,
            keep_recent=self._deps.agent_history_keep_recent,
            max_summary_tokens=compact_tokens,
        )
        # Notify the UI when history was actually compacted (message count dropped)
        # so the status bar's compression counter stays truthful.
        if len(messages) < msgs_before and self._deps.stream_manager and task_id:
            await self._deps.stream_manager.emit(task_id, "compaction", {})

    async def _maybe_emit_model(self, response: Any, emitted_model: str, task_id: str) -> str:
        """Emit a 'model' event when the answering model changes.

        Returns the model name now reflected in the UI (unchanged when no emit).
        """
        if response.model_used and response.model_used != emitted_model and self._deps.stream_manager and task_id:
            await self._deps.stream_manager.emit(task_id, "model", {"model": response.model_used})
            return response.model_used
        return emitted_model

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
            return call, result_str, not _is_rejection(decision)
        if call.name == "ask_user":
            result_str = await self._ask_user(payload, params)
            success = json.loads(result_str).get("success", False)
            return call, result_str, success
        # create_tool gates its own create/update actions behind an approval
        # card (see CreateToolTool._request_approval) - no special case here.
        # Default the workspace but respect an explicit model-supplied value  - 
        # same semantics as the orchestrator's direct-tool path.
        if payload.workspace and "workspace" not in params:
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
        reliability_lines = "\n".join(f"- {t.name} reliability: {score:.0%}" for t, score in scored_tools)
        now = localnow().strftime("%Y-%m-%d %H:%M %Z")
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
            f"## Handoff Directory\n{handoff_dir_for(payload.task_id)}\n\n"
            f"## Context\n{background or '(none)'}\n\n"
            f"## Tool reliability hints\n{reliability_lines or '(none)'}\n"
        )

    def _make_token_callback(self, task_id: str) -> Callable[[str], Awaitable[None]] | None:
        if self._deps.stream_manager is None or not task_id:
            return None
        return _TokenRelay(self._deps.stream_manager, task_id)

    async def _delegate_task(self, payload: AgentPayload, params: dict[str, Any]) -> str:
        """Run a specialist sub-agent and return its output as a tool result."""
        if payload.delegation_depth >= MAX_DELEGATION_DEPTH:
            return _failed_json(
                f"Delegation depth limit ({MAX_DELEGATION_DEPTH}) reached - "
                "you cannot delegate further. Write a final summary of what was "
                "accomplished, what was attempted, and what remains unresolved, "
                "then return that as your answer."
            )

        agent_name = str(params.get("agent", "general"))
        if agent_name in payload.delegation_chain:
            return _failed_json(
                f"Delegation cycle detected - '{agent_name}' is already in the "
                f"current chain {payload.delegation_chain}. "
                "Summarise what you have and return your best result instead of delegating again."
            )

        registry = self._deps.agent_registry
        if registry is None:
            return _failed_json("Agent registry not available for delegation.")

        task = str(params.get("task", ""))
        if not task:
            return _failed_json("delegate_task requires a non-empty 'task' parameter.")

        try:
            agent = registry.get(agent_name)
        except Exception:
            if agent_name in ENGINEERING_AGENTS:
                return _failed_json(
                    f"Engineering agent '{agent_name}' not found. "
                    "Cannot fall back to general for engineering tasks. "
                    "Ensure the agent is registered and retry."
                )
            try:
                agent = registry.get("general")
            except Exception:
                return _failed_json(f"Agent '{agent_name}' not found and no 'general' fallback.")

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
            logger.warning("Sub-agent '%s' raised in task '%s': %s", agent_name, payload.task_id, exc, exc_info=True)
            return _failed_json(str(exc))

    def _require_approval_store(self) -> Any:
        """Return the injected ApprovalStore or fail loudly if it is missing."""
        store = self._deps.approval_store
        if store is None:
            raise RuntimeError(
                f"Agent '{self.name}' needs an ApprovalStore for user interaction but "
                "none was injected into AgentDependencies. Wire it at startup."
            )
        return store

    async def _surface_card(
        self, payload: AgentPayload, *, card_type: CardType, title: str, body: str, options: list[str], event: CardEvent
    ) -> Card:
        """Build, optionally auto-resolve, and surface a card; return it resolved."""
        return await surface_card(
            store=self._require_approval_store(),
            stream_manager=self._deps.stream_manager,
            judgement_filter=self._deps.judgement_filter,
            timeout=self._deps.approval_timeout_seconds,
            agent_name=self.name,
            task_id=payload.task_id,
            card_type=card_type,
            title=title,
            body=body,
            options=options,
            event=event,
        )

    async def _request_approval(self, payload: AgentPayload, params: dict[str, Any]) -> str:
        """Ask the user to approve an irreversible action; return their decision."""
        card = await self._surface_card(
            payload,
            card_type=CardType.APPROVAL,
            title=f"{self.name.title()} - Approval Required",
            body=str(params.get("message", "Action requires your approval.")),
            options=list(params.get("options", list(APPROVAL_DEFAULT_OPTIONS))),
            event=CardEvent.APPROVAL,
        )
        return card.status

    async def _ask_user(self, payload: AgentPayload, params: dict[str, Any]) -> str:
        """Ask the user a clarifying question mid-loop and block until they answer.

        The card is a QUESTION and the return carries the user's actual answer (free
        text or a chosen option) so the agent continues with it instead of assuming.
        This is how an agent refuses to invent an unknown - it asks. A learned rule
        may answer it automatically (see :func:`surface_card`).
        """
        question = str(params.get("question", "")).strip()
        if not question:
            return _failed_json("ask_user requires a non-empty 'question'.")
        options = [str(o) for o in params.get("options", []) if str(o).strip()]

        card = await self._surface_card(
            payload,
            card_type=CardType.QUESTION,
            title=f"{self.name.title()} - Question",
            body=question,
            options=options,
            event=CardEvent.QUESTION,
        )
        answer = (card.chosen_option or "").strip()
        if not answer:
            return json.dumps({"success": False, "answered": False, "error": _ASK_USER_NO_ANSWER})
        return json.dumps({"success": True, "answered": True, "answer": answer})

    async def _call_tool(
        self,
        tool_map: dict[str, Tool],
        tool_name: str,
        params: dict[str, Any],
    ) -> str:
        if tool_name not in tool_map:
            return _failed_json(f"Tool '{tool_name}' not found. Available: {sorted(tool_map)}")
        try:
            result = await tool_map[tool_name].run(ToolInput(params=params))
            data = result.model_dump()
            if result.success:
                data["formatted"] = tool_map[tool_name].format_output(result.data or {})
        except Exception as exc:
            logger.warning("Tool '%s' raised: %s", tool_name, exc, exc_info=True)
            return _failed_json(str(exc))
        return _cap_tool_result(data)


def _cap_tool_result(data: dict[str, Any]) -> str:
    """Serialize a tool result, bounded to MAX_TOOL_RESULT_CHARS.

    A single large tool response must not exhaust the model's context window.
    Truncation happens *inside* the data dict so the JSON returned to the model
    is always syntactically valid: first each string field is capped, then  - 
    when non-string fields (large lists/dicts) still blow the budget - the
    whole data block is replaced with a bounded summary.
    """
    raw = json.dumps(data)
    if len(raw) <= MAX_TOOL_RESULT_CHARS:
        return raw

    omitted = len(raw) - MAX_TOOL_RESULT_CHARS
    inner = data.get("data", {})
    if isinstance(inner, dict):
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
    if len(raw) > MAX_TOOL_RESULT_CHARS:
        summary = json.dumps(data["data"])[: MAX_TOOL_RESULT_CHARS - _TOOL_RESULT_MIN_FIELD_CHARS]
        data["data"] = {"_truncated": summary + "…"}
        raw = json.dumps(data)
    return raw


class _TokenRelay:
    """Token callback that forwards streamed tokens to the SSE stream, splitting
    private reasoning (``<thought>…</thought>``) onto a separate channel.

    Answer text is emitted as ``token`` events (the unchanged contract); reasoning
    text is emitted as ``reasoning`` events so a UI can render it dimmed instead of
    treating it as the answer. Because tags can straddle token boundaries, a
    :class:`ReasoningStreamSplitter` buffers ambiguous fragments - :meth:`flush`
    releases anything still held when the stream ends.

    ``reset()`` is the optional protocol the ModelDispatcher uses after a
    mid-stream failover: it emits a ``stream_reset`` event so UIs discard the
    partial output streamed by the failed attempt, and starts a fresh splitter so
    no carry buffer leaks across the re-stream.
    """

    def __init__(self, stream_manager: Any, task_id: str) -> None:
        self._stream_manager = stream_manager
        self._task_id = task_id
        self._splitter = ReasoningStreamSplitter()

    async def _emit(self, segments: list[tuple[str, str]]) -> None:
        for channel, fragment in segments:
            event = "token" if channel == "answer" else "reasoning"
            await self._stream_manager.emit(self._task_id, event, {"text": fragment})

    async def __call__(self, token: str) -> None:
        await self._emit(self._splitter.feed(token))

    async def flush(self) -> None:
        """Release any text the splitter is still holding at end of stream."""
        await self._emit(self._splitter.flush())

    async def reset(self) -> None:
        self._splitter = ReasoningStreamSplitter()
        await self._stream_manager.emit(self._task_id, "stream_reset", {})


def _extract_success(tool_result_str: str) -> bool:
    try:
        return bool(json.loads(tool_result_str).get("success", False))
    except (json.JSONDecodeError, AttributeError):
        return False


def _failed_call(call: ToolCall, exc: BaseException) -> tuple[ToolCall, str, bool]:
    """Build a failed tool-call result from an exception raised during execution."""
    return call, _failed_json(str(exc)), False


def _failed_json(msg: str) -> str:
    return json.dumps({"success": False, "error": msg})


# Decisions that mean the action was not approved (a user reject, a model "reject",
# or a timeout treated as rejection).
_REJECTION_DECISIONS = frozenset({"reject", ApprovalDecision.REJECTED.value, ApprovalDecision.TIMEOUT_REJECTED.value})


def _is_rejection(decision: str) -> bool:
    return decision.lower() in _REJECTION_DECISIONS


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
    successful_tools: list[str] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
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
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tools_used": tools_used or [],
        # Always a list for agentic agents (even when empty) so the orchestrator
        # treats the output as verifiable; None would skip verification.
        "successful_tools": successful_tools if successful_tools is not None else [],
    }
