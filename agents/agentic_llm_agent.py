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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from agents.capabilities import build_platform_capabilities_summary
from agents.constants import (
    _TOOL_RESULT_MIN_FIELD_CHARS,
    MAX_TOOL_RESULT_CHARS,
)
from agents.context_compaction import (
    COMPACT_KEEP_RECENT_OVERFLOW,
    COMPACT_TOKENS_DEFAULT,
    COMPACT_TOKENS_HEAVY,
    HEAVY_OUTPUT_TOOLS,
    compact_history,
    compact_if_needed,
    estimate_messages_tokens,
)
from agents.llm_agent import LLMAgent
from agents.models import AgentPayload
from agents.reasoning import ReasoningStreamSplitter, strip_reasoning
from agents.schemas import ASK_USER_SCHEMA, FIND_TOOLS_SCHEMA, REQUEST_APPROVAL_SCHEMA
from agents.tool_output_reduction import salient_excerpt
from agents.tool_results import extract_success as _extract_success
from agents.tool_results import failed_json as _failed_json
from agents.tool_results import failure_kind as _failure_kind
from approval.interaction import CardEvent
from approval.models import Card, CardField, CardType, DecidedBy
from inference.cache_stats import CacheWasteTracker
from inference.exceptions import AllModelsRateLimitedError, ContextTooLargeError, is_model_unavailable_error
from inference.models import ToolCall, ToolCallRequest, ToolCallResponse
from tools._path import handoff_dir_for, personal_dir
from tools.base import Tool
from tools.models import ToolInput
from tools.output_spill import overflow_note, store_overflow
from tools.retrieval import tool_index_documents
from tools.universal.offered_before import OFFERED_SCOPE
from utils.execution_context import current_execution
from utils.tasks import spawn
from utils.text import normalize_dashes
from utils.time import RUNTIME_CONTEXT_INSTRUCTION, runtime_context
from utils.tools import tool_mutates

logger = logging.getLogger(__name__)

# Returned to the model when ask_user times out with no answer - steers it to be
# explicit about the gap rather than silently fabricating the missing detail.
_ASK_USER_NO_ANSWER = (
    "No answer received in time. Do not fabricate the missing detail - state the "
    "specific assumption you are forced to make, or stop and summarise exactly what "
    "you still need from the user."
)

# Agent-loop built-ins, not registry tools - excluded from tool-reliability tracking.
_INTERNAL_TOOLS = frozenset({"request_approval", "ask_user", "find_tools"})
# Questions on one ask_user card: enough for a flow's setup, few enough to answer at a glance.
_MAX_QUESTIONS = 8


# Soft limits guide the model toward synthesis; they never terminate a run.
# The global ``agent_max_iterations`` setting remains the hard safety ceiling.
_SOFT_BUDGETS: dict[str, tuple[int, int]] = {"quick_readonly": (3, 6)}

_QUICK_READONLY_POLICY = """## Quick read-only execution
This run is a bounded direct-answer inspection. Do not create handoff artifacts,
write files, or mutate external state. Put the useful findings in the
final answer. North records a compact manifest of successful evidence lookups
automatically, so do not duplicate tool output into an audit file.
"""

_EVIDENCE_LOCATOR_KEYS: tuple[str, ...] = (
    "path",
    "query",
    "pattern",
    "symbol",
    "url",
    "line",
    "start_line",
    "end_line",
)


# How much of a final answer is kept as its one-line summary.
_SUMMARY_CHARS = 120
# A step whose every model is rate limited is tried again this many times, waiting out each limit that says it
# lifts within the bound; past either, the run fails as before (and the task is queued for the model).
_RATE_LIMIT_WAITS = 3
_MAX_RATE_LIMIT_WAIT_SECONDS = 120.0
_RATE_LIMIT_GRACE_SECONDS = 1.0
# How a message you sent mid-task reaches the model (CODING_STYLE §15.4).
_MESSAGE_WHILE_WORKING = "The user sent this while you were working on the task. Take it into account from here on:"


def _tool_schemas(tool_map: dict[str, Tool], *, allow_questions: bool = True) -> list[dict]:
    """Every tool definition sent to the model this turn.

    Sorted by name, not by confidence. Tool definitions are the largest stable
    block of the cached prefix, and `_load_tools` orders them by a score that
    moves every time a tool succeeds or fails - so the ranking rewrote the prefix
    between runs and threw the cache away. The ranking still reaches the model,
    as text, in the reliability hints at the end of the task message where it
    costs nothing.
    """
    internal = [FIND_TOOLS_SCHEMA, *((REQUEST_APPROVAL_SCHEMA, ASK_USER_SCHEMA) if allow_questions else ())]
    return [tool_map[name].schema() for name in sorted(tool_map)] + internal


# What a run has done, as far as checking its answer goes. Cost and tokens are not carried: a stopped run's
# model calls are already in the ledger.
_EVIDENCE = (
    "tools_used",
    "successful_tools",
    "successful_tool_calls",
    "successful_tool_actions",
    "tool_call_count",
    "stopped_change",
    "left_for_user",
)


@dataclass
class _RunTally:
    """What one agent run has spent and used, and the answer it ends up returning."""

    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cache_waste: CacheWasteTracker = field(default_factory=CacheWasteTracker)
    last_tokens_in: int = 0
    last_model_used: str = ""
    tools_used: list[str] = field(default_factory=list)
    successful_tools: list[str] = field(default_factory=list)
    successful_tool_calls: dict[str, int] = field(default_factory=dict)
    successful_tool_actions: dict[str, int] = field(default_factory=dict)
    models_used: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    # The last change the run tried, when it was refused or failed and nothing changed after it.
    stopped_change: str | None = None
    # Whether the run left a card for the user to decide.
    left_for_user: bool = False

    def evidence(self) -> dict[str, Any]:
        """What the run did that its answer is checked against, saved with its progress."""
        return {name: getattr(self, name) for name in _EVIDENCE}

    def restore(self, evidence: dict[str, Any]) -> None:
        """Carry on from saved *evidence*: a run that continues is checked against all of its work."""
        for name in _EVIDENCE:
            if name in evidence:
                setattr(self, name, evidence[name])

    def add(self, response: ToolCallResponse) -> None:
        self.cost_usd += response.cost_usd
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        self.cached_tokens += response.cached_tokens
        self.cache_waste.add(
            tokens_in=response.tokens_in,
            cached_tokens=response.cached_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )
        self.last_tokens_in = response.tokens_in
        self.last_model_used = response.model_used
        if response.model_used:
            _append_once(self.models_used, response.model_used)

    def note_call(self, tool_name: str) -> None:
        self.tool_call_count += 1
        _append_once(self.tools_used, tool_name)

    def note_success(self, tool_name: str, params: dict[str, Any]) -> None:
        _append_once(self.successful_tools, tool_name)
        self.successful_tool_calls[tool_name] = self.successful_tool_calls.get(tool_name, 0) + 1
        action = params.get("action")
        if isinstance(action, str) and action.strip():
            key = f"{tool_name}:{action.strip().lower()}"
            self.successful_tool_actions[key] = self.successful_tool_actions.get(key, 0) + 1

    def note_outcome(self, tool_name: str, params: dict[str, Any], *, mutating: bool, success: bool) -> None:
        """What one call says about how the run ends: a change that did not happen, a card left for you."""
        if mutating:
            self.stopped_change = None if success else tool_name
        if success and tool_name == "request_approval" and params.get("wait") is False:
            self.left_for_user = True

    def outcome(self) -> str:
        """How the run ended, from what it did: never from the words of its answer.

        The answer's words were matched against regexes: "Yes. blocked" was badged
        BLOCKED, and any answer ending in a question WAITING FOR USER (issue #76,
        finding M10). Blocked means the last change it tried was refused or
        failed; waiting means it left you a card to decide.
        """
        if self.stopped_change is not None:
            return "blocked"
        return "waiting_for_user" if self.left_for_user else "response_ready"

    def answer(self, output: str, summary: str, *, outcome: str | None = None) -> dict[str, Any]:
        return _final_answer(
            output,
            summary,
            self.cost_usd,
            self.tools_used,
            self.successful_tools,
            self.tokens_in,
            self.tokens_out,
            models_used=self.models_used,
            cached_tokens=self.cached_tokens,
            cache_missed_tokens=self.cache_waste.totals.missed_tokens,
            cache_miss_count=self.cache_waste.totals.miss_count,
            evidence_counts=self.successful_tool_calls,
            evidence_actions=self.successful_tool_actions,
            outcome=outcome or self.outcome(),
        )


def _append_once(seen: list[str], value: str) -> None:
    """Keep first-seen order without a parallel set to go stale."""
    if value not in seen:
        seen.append(value)


def _estimate_value_tokens(value: Any) -> int:
    """Estimate serialized prompt tokens without retaining the underlying text."""
    if value in (None, "", [], {}):
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return max(1, (len(value) + 3) // 4)


class AgenticLLMAgent(LLMAgent):
    """LLMAgent that runs a ReAct loop via native function calling.

    Each iteration asks the model whether to call a tool or produce a final
    answer.  Text tokens from the final answer are forwarded to the SSE stream
    as they arrive so the UI can render them progressively.
    """

    async def _record_tool_call_confidence(self, tool_name: str, result_str: str, success: bool) -> None:
        """Record tool execution confidence if not a special internal tool.

        A tool that correctly reports absence, or an action a human declined, is
        not an unreliable tool. Counting those as failures ranked `read_file`
        below every other tool the researcher used, purely because it was asked
        whether an optional file existed.
        """
        if tool_name in _INTERNAL_TOOLS:
            return
        if not success and _failure_kind(result_str) != "error":
            return
        spawn(
            self._deps.confidence_tracker.record_use(self.name, tool_name, success),
            name=f"record_confidence:{self.name}/{tool_name}",
        )

    def _append_tool_call_exchange(
        self,
        messages: list[dict],
        results: list[tuple[ToolCall, str, bool, list[tuple[str, str]]] | tuple[ToolCall, str, bool]],
    ) -> None:
        """Format and append assistant tool_calls message and the respective tool outputs."""
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item[0].call_id,
                        "type": "function",
                        "function": {
                            "name": item[0].name,
                            "arguments": json.dumps(item[0].params),
                        },
                    }
                    for item in results
                ],
            }
        )
        user_visual_messages: list[dict] = []
        for item in results:
            call = item[0]
            result_str = item[1]
            images = item[3] if len(item) > 3 else []
            content = result_str
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": content,
                }
            )
            if images:
                content_blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": f"[Visual context from tool '{call.name}']"}
                ]
                for b64, mime in images:
                    content_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        }
                    )
                user_visual_messages.append(
                    {
                        "role": "user",
                        "content": content_blocks,
                    }
                )

        # Append any visual user messages after all tool responses to keep
        # all tool messages contiguous per OpenAI API protocol.
        messages.extend(user_visual_messages)

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
        # Where results enter the conversation, and the one place they are
        # bounded: every tool's answer and every error alike. The run history and the model then see the same text.
        results = [
            (call, _bound_tool_result(result_str, call.name), success, images)
            for call, result_str, success, images in results
        ]

        if payload.execution_profile == "quick_readonly":
            await self._record_quick_evidence(payload, results, tool_map)

        for call, result_str, success, _ in results:
            if self._deps.stream_manager and payload.task_id:
                await self._deps.stream_manager.emit(
                    payload.task_id, "tool_result", _tool_result_event(call.name, result_str, success)
                )
            await self._record_tool_call_confidence(call.name, result_str, success)

        self._append_tool_call_exchange(messages, results)
        return [(call.name, success) for call, _, success, _ in results]

    async def _record_quick_evidence(
        self,
        payload: AgentPayload,
        results: list[tuple[ToolCall, str, bool, list[tuple[str, str]]]],
        tool_map: dict[str, Tool],
    ) -> None:
        """Persist small evidence pointers for quick tasks, never fetched content."""
        references = []
        for call, _result, success, _images in results:
            if not success or self._is_mutating_call(call, tool_map):
                continue
            reference: dict[str, Any] = {"tool": call.name}
            for key in _EVIDENCE_LOCATOR_KEYS:
                value = call.params.get(key)
                if isinstance(value, str) and value:
                    if key == "url":
                        value = value.split("?", 1)[0].split("#", 1)[0]
                    reference[key] = value[:240]
                elif isinstance(value, int):
                    reference[key] = value
            references.append(reference)
        if not references:
            return

        data = {"profile": "quick_readonly", "references": references}
        if self._deps.stream_manager is not None:
            await self._deps.stream_manager.emit(payload.task_id, "evidence_manifest", data)
        elif self._deps.agent_run_store is not None:
            await self._deps.agent_run_store.record_event(
                payload.run_id,
                payload.task_id,
                "evidence_manifest",
                data,
            )

    async def _execute_calls_ordered(
        self,
        calls: list[ToolCall],
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> list[tuple[ToolCall, str, bool, list[tuple[str, str]]]]:
        """Run read-only calls concurrently and mutating calls sequentially.

        To preserve causal ordering (e.g. write_file before read_file in the same
        turn), calls are partitioned into contiguous chunks. Contiguous read-only
        calls run concurrently via asyncio.gather(), while mutating calls run
        sequentially under the per-WORKSPACE lock. Every call is wrapped so a
        raised exception becomes a failed tool result rather than cancelling its
        siblings (CODING_STYLE §10.5). Results preserve the original call order.
        """
        results: dict[int, tuple[ToolCall, str, bool, list[tuple[str, str]]]] = {}

        # Partition calls into contiguous runs of read-only vs mutating calls
        chunks: list[list[tuple[int, ToolCall]]] = []
        current_chunk: list[tuple[int, ToolCall]] = []
        current_is_mutating: bool | None = None

        for index, call in enumerate(calls):
            if call.name == "ask_user" and call.params.get("requires_user_action") is True:
                # A human takeover is a barrier, even to read-only browser calls:
                # do not inspect credentials or race the person while they act.
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
                chunks.append([(index, call)])
                current_is_mutating = None
                continue
            is_mut = self._is_mutating_call(call, tool_map)
            if current_is_mutating is None or is_mut == current_is_mutating:
                current_chunk.append((index, call))
                current_is_mutating = is_mut
            else:
                chunks.append(current_chunk)
                current_chunk = [(index, call)]
                current_is_mutating = is_mut
        if current_chunk:
            chunks.append(current_chunk)

        for chunk in chunks:
            is_mut = self._is_mutating_call(chunk[0][1], tool_map)
            if not is_mut:
                # Contiguous read-only calls execute concurrently
                gathered = await asyncio.gather(
                    *[self._safe_execute_call(c, payload, tool_map) for _, c in chunk],
                    return_exceptions=True,
                )
                for (index, call), outcome in zip(chunk, gathered, strict=True):
                    results[index] = outcome if not isinstance(outcome, BaseException) else _failed_call(call, outcome)
            else:
                # Mutating calls run one at a time, in order. Each takes the
                # workspace lock itself, for the change alone (Tool.execute):
                # never across an approval wait or a run it starts.
                for index, call in chunk:
                    results[index] = await self._safe_execute_call(call, payload, tool_map)

        ordered = [results[index] for index in range(len(calls))]
        return ordered

    def _before_change(self, payload: AgentPayload) -> Callable[[], Awaitable[None]] | None:
        """What a tool awaits just before it changes something: the task is marked as having acted.

        Before, not after, and it is allowed to fail the call: marked after a whole turn,
        a restart in the middle of one looked like a task that had done nothing, and ran it again.
        """
        store = self._deps.running_task_store
        if store is None or not payload.task_id:
            return None
        return partial(store.mark_side_effect, payload.task_id)

    async def _safe_execute_call(
        self,
        call: ToolCall,
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> tuple[ToolCall, str, bool, list[tuple[str, str]]]:
        """Execute one call, turning any unexpected exception into a failed result."""
        is_mutating = self._is_mutating_call(call, tool_map)
        if payload.execution_profile == "quick_readonly" and is_mutating:
            return _failed_call(
                call,
                RuntimeError("Mutating tools are unavailable in the quick read-only profile."),
            )
        if is_mutating and payload.mutation_policy == "deny":
            return _failed_call(
                call,
                RuntimeError("This flow skill step is read-only; mutating tools are blocked by policy."),
            )
        try:
            return await self._execute_call(call, payload, tool_map)
        except Exception as exc:
            if is_model_unavailable_error(exc):
                raise
            logger.warning("Tool call '%s' raised: %s", call.name, exc, exc_info=True)
            return _failed_call(call, exc)

    def _is_mutating_call(self, call: ToolCall, tool_map: dict[str, Tool]) -> bool:
        tool = tool_map.get(call.name)
        return bool(tool and tool_mutates(tool, call.params))

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        persona = await self._memory().read_persona()
        messages, tool_map, compact_tokens = self._init_conversation(payload, context, scored_tools, persona)
        tally = _RunTally()
        soft_budget_noted = False
        messages_read = 0
        first_step = 0
        if saved := await self._saved_progress(payload):
            messages, messages_read, first_step = saved["messages"], saved["messages_read"], saved["step"]
            tally.restore(saved["evidence"])

        known_registry_tools = (
            set(self._deps.tool_registry.all_tool_names()) if getattr(self._deps, "tool_registry", None) else set()
        )

        # Iteration cap is set from settings.agent_max_iterations via AgentDependencies.
        for iteration in range(first_step, self._deps.agent_max_iterations):
            await self._heartbeat(payload)
            messages_read = await self._read_your_messages(payload, messages, messages_read)
            if not soft_budget_noted:
                soft_budget_noted = await self._note_soft_budget_if_needed(
                    payload,
                    messages,
                    completed_turns=iteration,
                    tool_calls=tally.tool_call_count,
                )
            await self._compact_for_next_call(
                messages,
                tally.last_tokens_in,
                tally.last_model_used,
                compact_tokens,
                payload.task_id,
                deterministic_only=payload.execution_profile == "quick_readonly",
            )

            # Refresh tool_map each iteration so tools hot-loaded mid-task
            # (e.g. by create_tool) are immediately available to the LLM.
            _sync_hot_loaded_tools(self._deps, tool_map, known_registry_tools)
            visible_tools = (
                {name: tool for name, tool in tool_map.items() if not tool.is_mutating}
                if payload.execution_profile == "quick_readonly"
                else tool_map
            )
            tools = _tool_schemas(visible_tools, allow_questions=payload.allow_questions)
            token_cb = self._make_token_callback(payload.task_id)

            try:
                response = await self._complete_waiting_out_limits(messages, tools, payload, token_cb)
            except ContextTooLargeError:
                return tally.answer(
                    "Context window exceeded - the conversation is too long to continue.",
                    "Context overflow",
                    outcome="blocked",
                )

            # Stream finished - release any reasoning/answer fragment the splitter
            # withheld in case it began a tag that never completed.
            if token_cb is not None:
                await token_cb.flush()
            tally.add(response)
            await self._emit_prompt_profile(payload, iteration + 1, messages, tools, response)
            await self._record_provider_metadata(payload, response)

            if response.type == "message":
                # Final answer - answer tokens were already streamed via token_cb;
                # strip the model's private reasoning from the stored copy so it
                # matches the streamed view and never feeds extraction/verification.
                content = normalize_dashes(strip_reasoning(response.content or ""))
                if self._has_unread_messages(payload, messages_read):
                    # You wrote while it was answering: it answers again, with your message.
                    messages.append({"role": "assistant", "content": content})
                    if self._deps.stream_manager is not None:
                        await self._deps.stream_manager.emit(payload.task_id, "stream_reset", {})
                    continue
                if notes := await self._still_to_do(payload, tool_map):
                    # A step is left that must not depend on the model remembering it: it goes on, with the note.
                    messages += [{"role": "assistant", "content": content}, {"role": "user", "content": notes}]
                    if self._deps.stream_manager is not None:
                        await self._deps.stream_manager.emit(payload.task_id, "stream_reset", {})
                    continue
                return tally.answer(content, content[:_SUMMARY_CHARS])

            # Tool calls branch - execute the requested calls.
            if not response.calls:
                content = normalize_dashes(strip_reasoning(response.content or ""))
                return tally.answer(
                    content or "The model returned no tool calls and no message.",
                    "No actionable response",
                    outcome="blocked",
                )

            for call in response.calls:
                tally.note_call(call.name)
            evidence = await self._handle_tool_calls_response(response.calls, payload, tool_map, messages)
            if payload.execution_profile == "quick_readonly":
                if any(self._is_mutating_call(call, tool_map) for call in response.calls):
                    await self._escalate_execution_profile(payload, "mutating_tool")
                elif any(not success for _, success in evidence):
                    await self._escalate_execution_profile(payload, "tool_failure")
            for call, (name, success) in zip(response.calls, evidence, strict=True):
                if success:
                    tally.note_success(name, call.params)
                mutating = self._is_mutating_call(call, tool_map)
                tally.note_outcome(name, call.params, mutating=mutating, success=success)
            await self._save_progress(payload, messages, messages_read, iteration + 1, tally)

        return tally.answer(
            "Reached the maximum number of reasoning steps without a final answer.",
            "Iteration limit reached",
            outcome="blocked",
        )

    async def _still_to_do(self, payload: AgentPayload, tool_map: dict[str, Tool]) -> str:
        """What this run's tools say must happen before the task may end, or "" when nothing."""
        if payload.execution_profile == "quick_readonly":
            return ""
        workspace = payload.granted_workspace or payload.workspace or ""
        notes = [await tool.before_task_ends(payload.task_id, workspace) for tool in tool_map.values()]
        return "\n\n".join(note for note in notes if note)

    async def _save_progress(
        self, payload: AgentPayload, messages: list[dict], messages_read: int, step: int, tally: _RunTally
    ) -> None:
        """Save where the conversation has got to, after each step, so a stopped run continues from here.

        Resume started the agent over and redid the glob and every read it had made (issue #76, M19).
        """
        store = self._deps.agent_run_store
        if store is None:
            return
        state = {"messages": messages, "messages_read": messages_read, "step": step, "evidence": tally.evidence()}
        await store.save_checkpoint(payload.run_id, state)

    async def _saved_progress(self, payload: AgentPayload) -> dict[str, Any] | None:
        """Where this run had got to when it was stopped, if it is continuing (see `Agent.run`)."""
        store = self._deps.agent_run_store
        if store is None or payload.parent_run_id is not None:
            return None
        saved = await store.checkpoint(payload.run_id)
        if saved and self._deps.stream_manager is not None and payload.task_id:
            resumed = {"agent": self.name, "step": saved["step"]}
            await self._deps.stream_manager.emit(payload.task_id, "resumed", resumed)
        return saved

    async def _heartbeat(self, payload: AgentPayload) -> None:
        """Tell the stuck-task watchdog the task is still working: every step is progress.

        Beaten only when a stage started, a task working for hours through many steps
        read as stalled since its start.
        """
        store = self._deps.running_task_store
        if store is None or not payload.task_id:
            return
        try:
            await store.heartbeat(payload.task_id)
        except Exception:
            logger.debug("heartbeat failed for task %s", payload.task_id, exc_info=True)

    def _reads_messages(self, payload: AgentPayload) -> bool:
        """A task's run reads the messages you send it while it works."""
        return self._deps.task_inbox is not None and bool(payload.task_id)

    async def _read_your_messages(self, payload: AgentPayload, messages: list[dict], read: int) -> int:
        """Add what you sent the task after the first *read* messages to the conversation; returns the new count."""
        if not self._reads_messages(payload):
            return read
        assert self._deps.task_inbox is not None
        new = self._deps.task_inbox.read(payload.task_id, read)
        for text in new:
            messages.append({"role": "user", "content": f"{_MESSAGE_WHILE_WORKING}\n\n{text}"})
        if new and self._deps.stream_manager is not None:
            await self._deps.stream_manager.emit(payload.task_id, "user_message_read", {"count": len(new)})
        return read + len(new)

    def _has_unread_messages(self, payload: AgentPayload, read: int) -> bool:
        if not self._reads_messages(payload):
            return False
        assert self._deps.task_inbox is not None
        return self._deps.task_inbox.count(payload.task_id) > read

    async def _note_soft_budget_if_needed(
        self,
        payload: AgentPayload,
        messages: list[dict],
        *,
        completed_turns: int,
        tool_calls: int,
    ) -> bool:
        """Ask an over-budget quick run to synthesize, without forcing it to stop."""
        budget = _SOFT_BUDGETS.get(payload.execution_profile)
        if budget is None:
            return False
        turn_budget, tool_budget = budget
        if completed_turns < turn_budget and tool_calls < tool_budget:
            return False

        reason = "model_turns" if completed_turns >= turn_budget else "tool_calls"
        messages.append(
            {
                "role": "system",
                "content": (
                    "Soft efficiency budget reached. If the requested result is supported by the evidence already "
                    "collected, synthesize the final answer now. If material evidence is still missing, continue; "
                    "do not guess or stop solely because of this budget. Limit further work to the specific missing "
                    "evidence."
                ),
            }
        )
        if self._deps.stream_manager is not None and payload.task_id:
            await self._deps.stream_manager.emit(
                payload.task_id,
                "budget_soft_limit",
                {
                    "profile": payload.execution_profile,
                    "reason": reason,
                    "completed_turns": completed_turns,
                    "tool_calls": tool_calls,
                    "turn_budget": turn_budget,
                    "tool_budget": tool_budget,
                },
            )
        await self._escalate_execution_profile(payload, "soft_budget_exceeded")
        return True

    async def _escalate_execution_profile(self, payload: AgentPayload, reason: str) -> None:
        """Remove quick-path constraints when the task proves more involved."""
        if payload.execution_profile != "quick_readonly":
            return
        payload.execution_profile = "standard"
        if self._deps.stream_manager is not None and payload.task_id:
            await self._deps.stream_manager.emit(
                payload.task_id,
                "execution_profile_escalated",
                {"from": "quick_readonly", "to": "standard", "reason": reason},
            )

    async def _complete_waiting_out_limits(
        self,
        messages: list[dict],
        tools: list[dict],
        payload: AgentPayload,
        token_cb: ReasoningStreamSplitter | None,
    ) -> ToolCallResponse:
        """One model turn that waits out a short rate limit instead of failing the run.

        A failed run is retried from its first step, so a rate limit at step seven threw away six steps and
        ran their tools again; on a free tier that read as three failed runs before a fourth went through.
        A limit that says when it lifts, and soon, is waited out here and the same step tried again. One
        with no retry time (billing, nothing serves the part) or a long one still fails the run, as before.
        """
        for waited in range(_RATE_LIMIT_WAITS + 1):
            try:
                return await self._complete_or_shrink(messages, tools, payload, token_cb)
            except AllModelsRateLimitedError as exc:
                wait = exc.retry_after
                if wait is None or wait > _MAX_RATE_LIMIT_WAIT_SECONDS or waited == _RATE_LIMIT_WAITS:
                    raise
                logger.info(
                    "Agent %s: every model is rate limited; waiting %.0fs to retry step (task %s)",
                    self.name,
                    wait,
                    payload.task_id,
                )
                if token_cb is not None:
                    await token_cb.reset()
                await self._heartbeat(payload)
                await asyncio.sleep(max(wait, 0.0) + _RATE_LIMIT_GRACE_SECONDS)
        raise AssertionError("unreachable")  # the loop returns or raises

    async def _complete_or_shrink(
        self,
        messages: list[dict],
        tools: list[dict],
        payload: AgentPayload,
        token_cb: ReasoningStreamSplitter | None,
    ) -> ToolCallResponse:
        """One model turn, compacting the history once if the context overflows."""
        try:
            return await self._complete_with_tools(
                messages,
                tools,
                payload.task_id,
                token_cb,
            )
        except ContextTooLargeError:
            compact_history(messages, keep_recent=COMPACT_KEEP_RECENT_OVERFLOW)
            # Discard whatever the failed attempt streamed before re-streaming
            # so the splitter starts clean and UIs drop the partial output.
            if token_cb is not None:
                await token_cb.reset()
            return await self._complete_with_tools(
                messages,
                tools,
                payload.task_id,
                token_cb,
            )

    def _init_conversation(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
        persona: str = "",
    ) -> tuple[list[dict], dict[str, Tool], int]:
        """Build the initial system+user messages, tool map, and compaction budget.

        The persona (soul.md) leads the system prompt so north's voice frames the
        agent's own instructions.
        """
        preamble = f"{persona}\n\n" if persona else ""
        system_prompt = f"{preamble}{self._load_system_prompt()}\n\n{RUNTIME_CONTEXT_INSTRUCTION}"
        if payload.execution_profile == "quick_readonly":
            system_prompt = f"{system_prompt}\n\n{_QUICK_READONLY_POLICY}"
        if capabilities := build_platform_capabilities_summary(self._deps):
            system_prompt = f"{system_prompt}\n\n{capabilities}"
        user_text = self._build_task_message(payload, context, scored_tools)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        tool_map = {t.name: t for t, _ in scored_tools}
        # Agents with bash/git produce larger tool outputs; give their
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
        *,
        deterministic_only: bool = False,
    ) -> None:
        """Compact conversation history before the next API call.

        Token-aware: summarise old history when we approach the model's context
        window (percentage threshold) using live router context window resolution
        and estimated message token footprints.
        """
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
            deterministic_only=deterministic_only,
        )

        # Notify the UI when history was actually compacted (message count dropped)
        # so the status bar's compression counter stays truthful.
        if len(messages) < msgs_before and self._deps.stream_manager and task_id:
            await self._deps.stream_manager.emit(task_id, "compaction", {})

    async def _emit_prompt_profile(
        self,
        payload: AgentPayload,
        turn: int,
        messages: list[dict],
        tools: list[dict],
        response: ToolCallResponse,
    ) -> None:
        """Persist content-free token estimates for each prompt component."""
        stream = self._deps.stream_manager
        if stream is None or not payload.task_id:
            return

        system_tokens = estimate_messages_tokens(messages[:1])
        task_tokens = _estimate_value_tokens(payload.prompt)
        context_sections = {
            name: _estimate_value_tokens(value) for name, value in payload.context_sections.items() if value
        }
        initial_user_tokens = estimate_messages_tokens(messages[1:2]) if len(messages) > 1 else 0
        user_scaffolding = max(0, initial_user_tokens - task_tokens - sum(context_sections.values()))
        react_history = estimate_messages_tokens(messages[2:]) if len(messages) > 2 else 0
        tool_schema_tokens = _estimate_value_tokens(tools)
        sections = {
            "system_instructions": system_tokens,
            "task": task_tokens,
            **context_sections,
            "user_scaffolding": user_scaffolding,
            "react_history": react_history,
            "tool_schemas": tool_schema_tokens,
        }
        try:
            await stream.emit(
                payload.task_id,
                "prompt_profile",
                {
                    "turn": turn,
                    "estimated_input_tokens": sum(sections.values()),
                    "actual_input_tokens": response.tokens_in,
                    "output_tokens": response.tokens_out,
                    "cached_tokens": response.cached_tokens,
                    "cache_write_tokens": response.cache_write_tokens,
                    "sections": sections,
                },
            )
        except Exception:
            logger.debug("prompt_profile emit failed for task %s", payload.task_id, exc_info=True)

    async def _record_provider_metadata(self, payload: AgentPayload, response: Any) -> None:
        """Persist structured provider protocol state for this exact run."""
        metadata = getattr(response, "provider_metadata", None)
        if not isinstance(metadata, dict) or not metadata:
            return
        if self._deps.agent_run_store is not None:
            await self._deps.agent_run_store.merge_provider_state(payload.run_id, metadata)
        if self._deps.stream_manager is not None:
            await self._deps.stream_manager.emit(payload.task_id, "model_response", metadata)

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
                component=self.name,
                task_id=task_id,
                run_id=(current.run_id if (current := current_execution()) is not None else None),
            ),
            token_callback=token_callback,
        )

    async def _execute_call(
        self,
        call: ToolCall,
        payload: AgentPayload,
        tool_map: dict[str, Tool],
    ) -> tuple[ToolCall, str, bool, list[tuple[str, str]]]:
        """Execute one tool call and return (call, result_json, success, images)."""
        params = dict(call.params)
        if call.name in {"request_approval", "ask_user"} and not payload.allow_questions:
            return (
                call,
                _failed_json("This run may not ask the user: finish with what you have, and say what is still open."),
                False,
                [],
            )
        if call.name == "request_approval" and params.get("wait") is False:
            card = await self._hand_over_card(payload, params)
            if card is None:
                result_str = json.dumps(
                    {"left_for_user": False, "already_offered": True, "note": "Offered before; no card was made."}
                )
            else:
                result_str = json.dumps({"left_for_user": True, "card_id": card.id, "decision": card.status})
            return call, result_str, True, []
        if call.name == "request_approval":
            result_str = await self._ask_ahead(payload, params, tool_map)
            return call, result_str, bool(json.loads(result_str).get("approved")), []
        if call.name == "ask_user":
            result_str = await self._ask_user(payload, params)
            success = json.loads(result_str).get("success", False)
            return call, result_str, success, []
        if call.name == "find_tools":
            result_str = await self._find_tools(params, tool_map, payload.allowed_tools)
            success = json.loads(result_str).get("success", False)
            return call, result_str, success, []
        # Every tool call that changes something is put to the approval layer
        # by Tool.execute - no special case here.
        result_str, images = await self._call_tool(tool_map, call.name, params, payload)
        return call, result_str, _extract_success(result_str), images

    async def _find_tools(
        self,
        params: dict[str, Any],
        tool_map: dict[str, Tool],
        allowed_tools: list[str] | None = None,
    ) -> str:
        """Search the global catalog and expose matches on the next model turn."""
        query = str(params.get("query", "")).strip()
        if not query:
            return _failed_json("find_tools requires a non-empty 'query'.")
        try:
            limit = max(1, min(10, int(params.get("limit", 5))))
        except (TypeError, ValueError):
            limit = 5

        registry = getattr(self._deps, "tool_registry", None)
        if registry is None:
            return _failed_json("Tool registry is unavailable.")
        registry_tools = registry.available_tools()
        if allowed_tools is not None:
            allowed = set(allowed_tools)
            registry_tools = [tool for tool in registry_tools if tool.name in allowed]
        catalog = {tool.name: tool for tool in registry_tools}

        ranked: list[str] = []
        index = getattr(self._deps, "tool_index", None)
        if index is not None:
            try:
                await index.update_tools(tool_index_documents(registry_tools))
                ranked = await index.search_tools(query, top_k=limit)
            except Exception:
                logger.debug("find_tools semantic lookup failed", exc_info=True)

        exact_name = query.lower().replace("-", "_").replace(" ", "_")
        exact = [exact_name] if exact_name in catalog else []
        lexical = _lexical_tool_matches(query, registry_tools)
        names: list[str] = []
        for name in [*exact, *ranked, *lexical]:
            if name in catalog and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        # Configured tools that are not here: without this the agent could only guess why ("confirm that
        # you've restarted North") about an MCP server whose command did not exist.
        unavailable = sorted(registry.unavailable().values()) if hasattr(registry, "unavailable") else []
        if not names:
            if unavailable:
                return json.dumps(
                    {"success": False, "error": f"No tools matched {query!r}.", "not_available": unavailable}
                )
            return _failed_json(f"No tools matched {query!r}.")

        newly_loaded: list[str] = []
        for name in names:
            if name not in tool_map:
                tool_map[name] = catalog[name]
                newly_loaded.append(name)
        matches = [{"name": name, "description": catalog[name].description} for name in names]
        return json.dumps(
            {
                "success": True,
                "matches": matches,
                "loaded": newly_loaded,
                "message": "Loaded tools will be callable on the next turn.",
                **({"not_available": unavailable} if unavailable else {}),
            }
        )

    def _build_task_message(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> str:
        """User message without the tool list (tools are passed as function defs)."""
        reliability_lines = "\n".join(f"- {t.name} reliability: {score:.0%}" for t, score in scored_tools)
        system_lines = []
        if payload.workspace and payload.workspace != handoff_dir_for(payload.task_id):
            system_lines.append(f"- workspace: {payload.workspace}")
            system_lines += [f"- {note}" for tool, _ in scored_tools if (note := tool.task_note(payload.workspace))]
        else:
            # No folder was chosen: the task works in its own (CODING_STYLE §16.1.1). Called
            # "the workspace", it read as the user's: an agent reported "your workspace is
            # empty" and guessed /home/user/north for "this repo".
            system_lines.append(
                "- workspace: none chosen. This task works in its own folder (the Handoff Directory), "
                "which starts empty and is not the user's project. When the user means their project, "
                'repository or files ("this repo", "my workspace", "the notes folder"), ask which folder '
                "they mean (they can choose one in the composer) instead of guessing a path or calling "
                "your own folder theirs."
            )
        system_lines.append(
            "- interaction: when a requirement or a design decision is genuinely ambiguous, use "
            "ask_user rather than guessing. The approval mode decides who answers."
        )
        system_context = "## System Context\n" + "\n".join(system_lines) + "\n\n" if system_lines else ""

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
            f"## Task ID\n{payload.task_id}\n\n"
            f"## Handoff Directory\n{handoff_dir_for(payload.task_id)}\n\n"
            f"## Notes Directory\n{personal_dir('notes')}/{payload.task_id}\n\n"
            f"## Context\n{background or '(none)'}\n\n"
            f"## Tool reliability hints\n{reliability_lines or '(none)'}\n"
            f"\n{runtime_context()}\n\n"
            f"## Task\n{payload.prompt}\n"
        )

    def _make_token_callback(self, task_id: str) -> Callable[[str], Awaitable[None]] | None:
        if self._deps.stream_manager is None or not task_id:
            return None
        return _TokenRelay(self._deps.stream_manager, task_id)

    async def _surface_card(
        self,
        payload: AgentPayload,
        *,
        card_type: CardType,
        title: str,
        body: str,
        options: list[str],
        event: CardEvent,
        fields: list[CardField] | None = None,
        context: str = "",
        requires_user_action: bool = False,
    ) -> Card:
        """Raise a card through the approval layer and return it resolved."""
        interaction = self._deps.interaction
        if interaction is None:
            raise RuntimeError(
                f"Agent '{self.name}' needs the approval layer's interaction to ask the user, "
                "but none was injected into AgentDependencies. Wire it at startup."
            )
        card = Card.new(
            type=card_type,
            task_id=payload.task_id,
            agent=self.name,
            title=title,
            message=body,
            options=options,
            fields=fields or [],
            context=context,
            requires_user_action=requires_user_action,
        )
        return await interaction.request_decision(card, event=event)

    async def _ask_ahead(self, payload: AgentPayload, params: dict[str, Any], tool_map: dict[str, Tool]) -> str:
        """`request_approval`: ask once, through the approval layer, about the exact calls one thing takes.

        The card was the agent's own, beside the approval layer, and the tool then asked again: an approved
        "Delete old_safe.log" was followed by bash asking (issue #76, A14; CODING_STYLE §7.3).
        """
        calls = params.get("calls")
        if not isinstance(calls, list) or not calls or not all(isinstance(call, dict) for call in calls):
            return _failed_json(
                "Name in `calls` each tool call this approval is for, as you will make it. A single call needs no "
                "request_approval: make it, and north asks if your mode requires it. For a question, use ask_user."
            )
        named = []
        for call in calls:
            name = str(call.get("tool") or "")
            if name not in tool_map:
                return _failed_json(f"{name or 'A call'} is not one of your tools: {sorted(tool_map)}.")
            named.append((tool_map[name], _tool_input(payload, dict(call.get("params") or {}))))
        decision = await self._deps.tool_registry.ask_ahead(
            named,
            task_id=payload.task_id,
            title=str(params.get("title") or "Approve what north is about to do")[:160],
            message=str(params.get("message") or ""),
        )
        decided_by = _ANSWERED_BY.get(decision.decided_by, "north; the user has not seen this card")
        if not decision.allowed:
            return json.dumps(
                {
                    "approved": False,
                    "decided_by": decided_by,
                    "why": decision.reason,
                    "next": "Do not make these calls.",
                }
            )
        return json.dumps(
            {
                "approved": True,
                "decided_by": decided_by,
                "next": "Make exactly these calls now: each runs once without asking again; any other call asks.",
            }
        )

    async def _hand_over_card(self, payload: AgentPayload, params: dict[str, Any]) -> Card | None:
        """Leave finished work for the user without waiting: `request_approval` with ``wait: false``.

        A blocking card holds the run until the user answers, so a run preparing five applications waited on
        each in turn. The card outlives the task and the user decides whenever. `UserInteraction.hand_over`
        existed for this, but no agent could reach it.

        With `item_keys` the item is offered once: None when any key was offered before. Keeping that list
        was left to the agent, which wrote it somewhere else, or not at all, run after run.
        """
        interaction = self._deps.interaction
        if interaction is None:
            raise RuntimeError(
                f"Agent '{self.name}' needs the approval layer's interaction to leave work for the user, "
                "but none was injected into AgentDependencies. Wire it at startup."
            )
        raw_fields = params.get("fields") or []
        if not isinstance(raw_fields, list) or len(raw_fields) > 100:
            raise ValueError("request_approval fields must be a list of at most 100 items")
        keys = [str(key) for key in params.get("item_keys") or [] if str(key).strip()]
        offered = self._deps.offered_store if keys else None
        scope = OFFERED_SCOPE  # one memory for everything left for the user, whichever flow found it
        if offered is not None and await asyncio.to_thread(offered.seen, scope, *keys):
            return None
        card = await interaction.hand_over(
            task_id=payload.task_id,
            agent=self.name,
            source=f"agent:{self.name}",
            title=str(params.get("title") or f"{self.name.title()} - Ready for you")[:160],
            message=str(params.get("message", "Ready for your review.")),
            fields=[CardField.model_validate(item) for item in raw_fields],
            context=str(params.get("context") or "")[:50_000],
        )
        if offered is not None:
            await asyncio.to_thread(offered.remember, scope, *keys)
        return card

    async def _ask_user(self, payload: AgentPayload, params: dict[str, Any]) -> str:
        """Ask the user a clarifying question mid-loop and block until they answer.

        The card is a QUESTION and the return carries the user's actual answer (free
        text or a chosen option) so the agent continues with it instead of assuming.
        This is how an agent refuses to invent an unknown - it asks. In the modes
        that answer for you, the approval layer answers it (`ApprovalPolicy.answer`).
        Real-world handoffs use that same policy; an answer is not proof the
        physical action happened, so the caller must verify the resulting state.
        """
        question = str(params.get("question", "")).strip()
        requires_user_action = params.get("requires_user_action", False)
        if not isinstance(requires_user_action, bool):
            return _failed_json("requires_user_action must be a boolean.")
        if params.get("questions"):
            if requires_user_action:
                return _failed_json(
                    "A handoff (requires_user_action) is asked on its own: call ask_user with `question` alone."
                )
            return await self._ask_several(payload, question, params["questions"])
        if not question:
            return _failed_json("ask_user requires a non-empty 'question', or several in 'questions'.")
        options = [str(o) for o in params.get("options", []) if str(o).strip()]

        card = await self._surface_card(
            payload,
            card_type=CardType.QUESTION,
            title=f"{self.name.title()} - Question",
            body=question,
            options=options,
            event=CardEvent.QUESTION,
            requires_user_action=requires_user_action,
        )
        answer = (card.chosen_option or "").strip()
        if not answer:
            return json.dumps({"success": False, "answered": False, "error": _ASK_USER_NO_ANSWER})
        return json.dumps({"success": True, "answered": True, "answer": answer, "answered_by": _answered_by(card)})

    async def _ask_several(self, payload: AgentPayload, lead: str, raw: Any) -> str:
        """Ask several questions on one card and return every answer at once.

        Asked one call at a time, setting up a flow took five exchanges; on one card the user
        answers them together. Each question is an editable field whose options are suggestions.
        """
        if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_QUESTIONS:
            return _failed_json(f"'questions' must be a list of 1 to {_MAX_QUESTIONS} questions.")
        fields: list[CardField] = []
        for number, item in enumerate(raw, 1):
            text = str(item.get("question") or "").strip() if isinstance(item, dict) else ""
            if not text:
                return _failed_json(f"Question {number} in 'questions' needs a non-empty 'question'.")
            options = [str(o).strip() for o in item.get("options") or [] if str(o).strip()]
            fields.append(CardField(name=f"q{number}", label=text, value="", editable=True, options=options))
        card = await self._surface_card(
            payload,
            card_type=CardType.QUESTION,
            title=f"{self.name.title()} - Questions",
            body=lead or "Answer these together, then send them.",
            options=[],
            event=CardEvent.QUESTION,
            fields=fields,
        )
        answers = card.response or {}
        if all(str(answers.get(field.name) or "").strip() for field in fields):
            listed = [{"question": field.label, "answer": str(answers[field.name]).strip()} for field in fields]
            return json.dumps({"success": True, "answered": True, "answers": listed, "answered_by": _answered_by(card)})
        reply = (card.chosen_option or "").strip()
        if not reply:
            return json.dumps({"success": False, "answered": False, "error": _ASK_USER_NO_ANSWER})
        return json.dumps(
            {
                "success": True,
                "answered": True,
                "reply": reply,
                "note": "The user answered in one message: read each answer from it, and ask only what it leaves open.",
                "answered_by": _answered_by(card),
            }
        )

    async def _call_tool(
        self,
        tool_map: dict[str, Tool],
        tool_name: str,
        params: dict[str, Any],
        payload: AgentPayload,
    ) -> tuple[str, list[tuple[str, str]]]:
        if tool_name not in tool_map:
            return _failed_json(f"Tool '{tool_name}' not found. Available: {sorted(tool_map)}"), []
        tool_input = _tool_input(payload, params).model_copy(update={"before_change": self._before_change(payload)})
        try:
            result = await tool_map[tool_name].execute(tool_input)
            images: list[tuple[str, str]] = []
            if result.success:
                if result.data and "base64_image" in result.data and "mime_type" in result.data:
                    b64 = result.data.pop("base64_image")
                    mime = result.data.pop("mime_type")
                    if b64 and mime:
                        images.append((b64, mime))
                data = result.model_dump()
                # Ensure they are also popped from the serialized data dict
                if data.get("data"):
                    data["data"].pop("base64_image", None)
                    data["data"].pop("mime_type", None)
                data["formatted"] = tool_map[tool_name].format_output(result.data or {})
            else:
                data = result.model_dump()
        except Exception as exc:
            logger.warning("Tool '%s' raised: %s", tool_name, exc, exc_info=True)
            return _failed_json(str(exc)), []
        return json.dumps(data), images


_EMPTY: tuple[Any, ...] = (None, "", [], {})


# Who answered a card, as the agent is told it. Told only the answer, an agent
# reported north's yolo answers as yours: "you've said you'll delete it yourself",
# "You approved the deletion", for cards you never saw.
_ANSWERED_BY: dict[str, str] = {
    DecidedBy.YOU: "the user",
    DecidedBy.REPLAY: "north, replaying the user's own answer to the same card",
    DecidedBy.SAFE_LIST: "north's safe list; the user has not seen this card",
    DecidedBy.MEMORY_DECIDER: "north's memory decider, for the user; the user has not seen this card",
    DecidedBy.YOLO: "north in yolo mode, which says yes to everything; the user has not seen this card",
    DecidedBy.NORTH: "north's fixed rules; the user has not seen this card",
}


def _tool_input(payload: AgentPayload, params: dict[str, Any]) -> ToolInput:
    """A tool call's input as this run makes it: the task's folder and id unless the call names its own.

    Authority (the folders it may write) travels beside the model's params, never inside them.
    """
    if payload.workspace and "workspace" not in params:
        params["workspace"] = payload.workspace
    if payload.task_id and "task_id" not in params:
        params["task_id"] = payload.task_id
    return ToolInput(params=params, edit_scope=payload.edit_scope, granted_workspace=payload.granted_workspace or None)


def _answered_by(card: Card) -> str:
    return _ANSWERED_BY.get(card.decided_by or DecidedBy.YOU, "north; the user has not seen this card")


def _tool_result_event(tool_name: str, result_str: str, success: bool) -> dict[str, Any]:
    """The run-history record of one tool result: what the model was shown.

    A tool's readable `formatted` text stands for its data. Without one, the rest
    of the result is kept: a failed skill test recorded only "Skill selection tests
    failed.", though the model was told which prompts failed, and an `ask_user`
    result recorded no answer at all.
    """
    event: dict[str, Any] = {"tool": tool_name, "success": success}
    try:
        parsed = json.loads(result_str)
        if formatted := parsed.get("formatted"):
            event["formatted"] = formatted
        else:
            event.update({key: value for key, value in parsed.items() if key not in event and value not in _EMPTY})
    except (json.JSONDecodeError, AttributeError):
        pass
    return event


def _bound_tool_result(result: str, tool_name: str) -> str:
    """*result* as the model is given it: within MAX_TOOL_RESULT_CHARS, the rest kept in the overflow store."""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        data = None
    return _cap_tool_result(data if isinstance(data, dict) else {"output": result}, tool_name)


def _cap_tool_result(data: dict[str, Any], tool_name: str = "") -> str:
    """Serialize a tool result, bounded to MAX_TOOL_RESULT_CHARS.

    A single large tool response must not exhaust the model's context window.
    Truncation happens *inside* the result so the JSON returned to the model
    is always syntactically valid: first every long string is shortened, then
    - when large lists or dicts still blow the budget - all of it but its
    status fields is replaced with one bounded excerpt.

    Every string counts, wherever it sits. A tool's readable `formatted` copy
    and its `error` sit beside `data`, and a cap that shortened only `data`
    let a 250 KB file through whole: 153k input tokens a call instead of 29k,
    resent on every later step, and a 30 MB file ended its task with "the
    conversation is too long".

    What does not fit is kept, not dropped. The full serialization goes to the
    overflow store and the note carries its handle, so ``read_tool_output`` can
    search or page the remainder. Cutting a long output down to its head lost
    whatever was in the middle - a grep match, a value in the body of a page -
    and the agent had no way to know, because a truncated result reads exactly
    like a complete one.
    """
    raw = json.dumps(data)
    if len(raw) <= MAX_TOOL_RESULT_CHARS:
        return raw

    handle = store_overflow(tool_name or "tool", raw)
    total = len(raw)
    inner = data.get("data")
    places = [data, inner] if isinstance(inner, dict) else [data]
    strings = [(place, key) for place in places for key, value in place.items() if isinstance(value, str)]
    per_field = max(
        _TOOL_RESULT_MIN_FIELD_CHARS,
        (MAX_TOOL_RESULT_CHARS - _TOOL_RESULT_MIN_FIELD_CHARS) // max(len(strings), 1),
    )
    for place, key in strings:
        if len(place[key]) > per_field:
            place[key] = salient_excerpt(place[key], per_field)
    note = overflow_note(handle, MAX_TOOL_RESULT_CHARS, total)
    data["_handle"] = handle
    data["_note"] = note
    raw = json.dumps(data)
    if len(raw) > MAX_TOOL_RESULT_CHARS:
        # Trim against the *serialized* length, not an estimate of it: escaping
        # a summary back into JSON can double its size, so a budget computed up
        # front overshoots and the "bounded" result is not bounded. The status
        # fields stay as they are: whether the call succeeded, how it failed.
        status = {key: value for key, value in data.items() if key in _OVERFLOW_KEYS or _is_status_field(value)}
        summary = json.dumps({key: value for key, value in data.items() if key not in status})
        while True:
            raw = json.dumps({**status, "_reduced": salient_excerpt(summary, len(summary))})
            overshoot = len(raw) - MAX_TOOL_RESULT_CHARS
            if overshoot <= 0 or len(summary) <= _TOOL_RESULT_MIN_FIELD_CHARS:
                break
            summary = salient_excerpt(
                summary,
                max(_TOOL_RESULT_MIN_FIELD_CHARS, len(summary) - overshoot),
            )
    return raw


_OVERFLOW_KEYS = ("_handle", "_note")


def _is_status_field(value: Any) -> bool:
    """A flag, a count or a short code such as ``failure_kind``."""
    if isinstance(value, str):
        return len(value) <= _TOOL_RESULT_MIN_FIELD_CHARS
    return value is None or isinstance(value, bool | int | float)


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
            await self._stream_manager.emit(self._task_id, event, {"text": normalize_dashes(fragment)})

    async def __call__(self, token: str) -> None:
        await self._emit(self._splitter.feed(token))

    async def flush(self) -> None:
        """Release any text the splitter is still holding at end of stream."""
        await self._emit(self._splitter.flush())

    async def reset(self) -> None:
        self._splitter = ReasoningStreamSplitter()
        await self._stream_manager.emit(self._task_id, "stream_reset", {})


def _failed_call(call: ToolCall, exc: BaseException) -> tuple[ToolCall, str, bool, list[tuple[str, str]]]:
    """Build a failed tool-call result from an exception raised during execution."""
    return call, _failed_json(str(exc)), False, []


def _sync_hot_loaded_tools(
    deps: Any,
    tool_map: dict[str, Tool],
    known_registry_tools: set[str],
) -> None:
    """Add any newly registered tools (e.g. created by create_tool) since execution started."""
    registry = getattr(deps, "tool_registry", None)
    if registry is None:
        return
    for tool in registry.available_tools():
        if tool.name not in known_registry_tools:
            known_registry_tools.add(tool.name)
            tool_map[tool.name] = tool


def _lexical_tool_matches(query: str, tools: Sequence[Tool]) -> list[str]:
    """Rank catalog tools without embeddings, favoring exact name matches."""
    query_words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query.lower()))
    normalized_query = query.lower().replace("-", "_").replace(" ", "_")
    scored: list[tuple[int, str]] = []
    for tool in tools:
        name = tool.name.lower()
        name_words = set(name.split("_"))
        description_words = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", tool.description.lower()))
        score = 0
        if name == normalized_query or name in query_words:
            score += 100
        score += 5 * len(query_words & name_words)
        score += len(query_words & description_words)
        if score:
            scored.append((score, tool.name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored]


def _final_answer(
    output: str,
    summary: str,
    cost_usd: float = 0.0,
    tools_used: list[str] | None = None,
    successful_tools: list[str] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    models_used: list[str] | None = None,
    cached_tokens: int = 0,
    cache_missed_tokens: int = 0,
    cache_miss_count: int = 0,
    evidence_counts: dict[str, int] | None = None,
    evidence_actions: dict[str, int] | None = None,
    outcome: str = "response_ready",
) -> dict[str, Any]:
    return {
        "output": output,
        "summary": summary,
        "data": {
            "evidence_counts": evidence_counts or {},
            "evidence_actions": evidence_actions or {},
            "outcome_status": outcome,
        },
        "requires_approval": False,
        "has_question": False,
        "question": None,
        "question_options": [],
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cached_tokens": cached_tokens,
        "cache_missed_tokens": cache_missed_tokens,
        "cache_miss_count": cache_miss_count,
        "tools_used": tools_used or [],
        # Always a list for agentic agents (even when empty) so the orchestrator
        # treats the output as verifiable; None would skip verification.
        "successful_tools": successful_tools if successful_tools is not None else [],
        "models_used": models_used or [],
    }
