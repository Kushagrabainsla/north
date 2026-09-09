"""Context compaction utilities for the agent ReAct loop.

Responsible for keeping the conversation history within the model's context
window by summarising old tool-call exchanges via the LLM or falling back to
simple truncation when summarisation is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from inference.models import CompletionRequest, PoolPriority
from tools.output_spill import carries_handle, overflow_note, store_overflow, summary_note
from utils.prompts import load_prompt

logger = logging.getLogger(__name__)

# Compact when token usage hits this fraction of the context window.
COMPACTION_THRESHOLD = 0.75

# Agents with these tools produce larger, denser outputs (file contents, diffs,
# bash stdout). Their summaries need more room to preserve file paths and errors.
HEAVY_OUTPUT_TOOLS: frozenset[str] = frozenset({"bash", "git", "patch_file"})
COMPACT_TOKENS_DEFAULT = 512  # ~350 words - general agents
COMPACT_TOKENS_HEAVY = 1000  # ~700 words - agents with bash/git/patch_file
# keep_recent used when context overflows every available model's window.
COMPACT_KEEP_RECENT_OVERFLOW: int = 1
# Max chars per field/line kept when rendering history for summarisation.
_RENDER_PREVIEW_CHARS: int = 200
# Thresholds for truncating large tool outputs during history compaction.
_COMPACT_TRUNCATE_THRESHOLD: int = 500  # skip outputs shorter than this
_COMPACT_TRUNCATE_KEEP: int = 300  # chars kept from oversized outputs
# Words allowed in the summary that replaces one retired tool result. Larger
# than a history summary because this stands in for a single concrete output -
# a file listing, a search result - where the specifics are the whole value.
_TOOL_SUMMARY_MAX_WORDS: int = 220
_TOOL_SUMMARY_MAX_TOKENS: int = 400
# Ceiling on what is sent to the summariser. In practice it never binds:
# `_cap_tool_result` caps a result at 40k chars before it ever enters history,
# so this only guards against a caller that bypassed it.
_TOOL_SUMMARY_INPUT_MAX_CHARS: int = 60_000

# Last resort, used only when no router is available to answer from fetched
# facts (see ModelDispatcher.get_context_window). A published window is a fact
# north downloads; this table is a guess that ages badly, so it exists to keep
# compaction working in isolation - tests, offline tools - and nowhere else.
# Ordered from most-specific to least-specific so the first match wins.
_CONTEXT_WINDOW_TABLE: tuple[tuple[str, int], ...] = (
    ("gemini-2", 1_000_000),
    ("gemini-1.5", 1_000_000),
    ("gemini", 128_000),
    ("claude", 200_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("gpt-5", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4.1", 128_000),
    ("ox-alpha", 128_000),
    ("0x-alpha", 128_000),
    ("0xalpha", 128_000),
    ("stealth", 128_000),
    ("deepseek", 128_000),
    ("qwen", 128_000),
    ("llama", 128_000),
    ("mistral", 128_000),
    ("kimi", 128_000),
    ("glm", 128_000),
    ("minimax", 128_000),
    ("gemma", 128_000),
    ("phi", 16_000),
)
_DEFAULT_CONTEXT_WINDOW = 128_000


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate the total token count of a message list before calling the API.

    Approximates tokens as ~4 chars per token for all roles, message contents,
    tool call names/arguments, and tool return payloads.
    """
    total_chars = 0
    for m in messages:
        total_chars += len(str(m.get("role", "")))
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total_chars += len(str(fn.get("name", "")))
            total_chars += len(str(fn.get("arguments", "")))
    return max(1, total_chars // 4)


def context_window_for(model: str, router: Any = None) -> int:
    """Return the published context-window size (tokens) for a model identifier.

    Asks the router first: it answers from merged model facts, which join across
    catalog sources on the canonical id, so a model whose own provider publishes
    nothing still gets its real window. The name table below is the fallback for
    when there is no router at all.
    """
    if router is not None and hasattr(router, "get_context_window"):
        try:
            window = router.get_context_window(model)
            if window and window > 0:
                return window
        except Exception:
            pass

    m = model.lower()
    for fragment, size in _CONTEXT_WINDOW_TABLE:
        if fragment in m:
            return size
    return _DEFAULT_CONTEXT_WINDOW


def _is_visual_context_message(msg: dict) -> bool:
    """Return True if msg is a synthetic visual-context user message containing image data."""
    content = msg.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                return True
    return False


def exchange_boundaries(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (start, end_inclusive) index pairs for each tool-call exchange.

    An exchange = one assistant message that has tool_calls + all the tool
    result messages (and any accompanying visual user blocks) that immediately
    follow it.
    """
    exchanges: list[tuple[int, int]] = []
    i = 2  # skip [0]=system, [1]=user-task
    while i < len(messages):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            start = i
            j = i + 1
            while j < len(messages):
                role = messages[j].get("role")
                if role == "tool" or role == "user" and _is_visual_context_message(messages[j]):
                    j += 1
                else:
                    break
            exchanges.append((start, j - 1))
            i = j
        else:
            i += 1
    return exchanges


def render_exchange_for_summary(messages: list[dict]) -> str:
    """Format a slice of the message list into a short readable string for summarisation."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            content_str = str(msg.get("content", ""))
            if "## Earlier context (auto-compacted)" in content_str:
                lines.append(f"[previous summary:\n{content_str}]")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                    args_str = json.dumps(args)[:_RENDER_PREVIEW_CHARS]
                except Exception:
                    args_str = str(fn.get("arguments", ""))[:_RENDER_PREVIEW_CHARS]
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
                lines.append(f"  ← result: {str(content)[:_RENDER_PREVIEW_CHARS]}")
        elif role == "user":
            content_str = str(msg.get("content", ""))
            if "## Earlier context (auto-compacted)" in content_str:
                lines.append(f"[previous summary:\n{content_str}]")
            else:
                lines.append(f"[user context: {content_str[:_RENDER_PREVIEW_CHARS]}]")
    return "\n".join(lines)


def _already_shrunk(content: str) -> bool:
    """Whether this result has already been retired once.

    A shrunk result carries its handle, and the marker explaining the handle can
    push it back over the size threshold - so without this check the next
    compaction pass shrinks the shrunken copy, spilling the truncated text as a
    new entry and evicting the original it points at.
    """
    return carries_handle(content)


def _shrinkable_indices(messages: list[dict], indices: list[int]) -> list[int]:
    """The retired results worth shrinking: big enough, and not already done."""
    keep = []
    for idx in indices:
        content = messages[idx].get("content")
        if not isinstance(content, str) or len(content) <= _COMPACT_TRUNCATE_THRESHOLD:
            continue
        if _already_shrunk(content):
            continue
        keep.append(idx)
    return keep


def _replace_with_summary(msg: dict, handle: str, original: str, summary: str) -> None:
    """Put *summary* where the output was, keeping the route back to the original."""
    note = summary_note(handle, len(original))
    try:
        data = json.loads(original)
    except Exception:
        data = None
    if isinstance(data, dict):
        minimal: dict[str, Any] = {}
        if "success" in data:
            minimal["success"] = data["success"]
        if "error" in data:
            minimal["error"] = data["error"]
        minimal["summary"] = summary
        minimal["_handle"] = handle
        minimal["_note"] = note
        msg["content"] = json.dumps(minimal)
        return
    msg["content"] = f"{summary}\n\n[{note}]"


def _replace_with_head(msg: dict, handle: str, original: str) -> None:
    """Keep the first few hundred characters and a pointer to the rest.

    The fallback, used when no model is available to summarise. It is the weakest
    thing north does with an oversized result and the reason `read_tool_output`
    exists - it says nothing about the part it drops.
    """
    try:
        data = json.loads(original)
    except Exception:
        data = None
    if isinstance(data, dict):
        minimal: dict[str, Any] = {}
        if "success" in data:
            minimal["success"] = data["success"]
        if "error" in data:
            minimal["error"] = data["error"]
        minimal["_handle"] = handle
        minimal["_note"] = overflow_note(handle, 0, len(original))
        msg["content"] = json.dumps(minimal)
        return
    kept = original[:_COMPACT_TRUNCATE_KEEP]
    msg["content"] = f"{kept}... [{overflow_note(handle, len(kept), len(original))}]"


def _shrink_paired_assistant_args(messages: list[dict], indices: list[int]) -> None:
    """Drop the arguments of the assistant call each retired result answered.

    Both halves of a finished exchange shrink together; keeping a large argument
    payload for a result that is no longer there is pure cost.
    """
    call_id_to_assistant: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id")
                if cid:
                    call_id_to_assistant[cid] = i

    compacted_assistant: set[int] = set()
    for idx in indices:
        call_id = messages[idx].get("tool_call_id")
        if not call_id or call_id not in call_id_to_assistant:
            continue
        ast_idx = call_id_to_assistant[call_id]
        if ast_idx in compacted_assistant:
            continue
        for tc in messages[ast_idx].get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            if isinstance(args, str) and len(args) > _RENDER_PREVIEW_CHARS:
                fn["arguments"] = "{}"
        compacted_assistant.add(ast_idx)


def _truncate_tool_messages(messages: list[dict], indices_to_compact: list[int]) -> None:
    """Shrink retired results by keeping a head. Used when no model is available.

    Keeps the full text in the overflow store first, so retiring an old result
    is reversible. The agent has already read these, but "already read" is not
    "still remembered" once the history it lived in has been compacted away.
    """
    for idx in _shrinkable_indices(messages, indices_to_compact):
        content = messages[idx]["content"]
        _replace_with_head(messages[idx], store_overflow("history", content), content)
    _shrink_paired_assistant_args(messages, indices_to_compact)


def _task_text(messages: list[dict]) -> str:
    """The user's task, which makes a summary question-aware rather than generic.

    A summary written without knowing what is being asked keeps what looks
    important instead of what is needed - which is the failure truncation had,
    reached a more expensive way.
    """
    for msg in messages[:2]:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"][:2000]
    return "(task not recorded)"


def _tool_name_for(messages: list[dict], idx: int) -> str:
    """The tool whose result sits at *idx*, for the summariser's context."""
    call_id = messages[idx].get("tool_call_id")
    if not call_id:
        return "a tool"
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == call_id:
                return str(tc.get("function", {}).get("name") or "a tool")
    return "a tool"


async def _summarise_one_result(
    *,
    original: str,
    task: str,
    tool_name: str,
    inference_router: Any,
    component: str,
    task_id: str | None,
) -> str:
    """A question-aware summary of one retired tool result, or "" if unavailable."""
    prompt = load_prompt("prompts/tool_output_summary.md").format(
        task=task,
        tool_name=tool_name,
        output=original[:_TOOL_SUMMARY_INPUT_MAX_CHARS],
        max_words=_TOOL_SUMMARY_MAX_WORDS,
    )
    try:
        resp = await inference_router.complete(
            CompletionRequest(
                prompt=prompt,
                priority=PoolPriority.LOW,
                component=f"{component}:tool-summary",
                task_id=task_id,
                max_tokens=_TOOL_SUMMARY_MAX_TOKENS,
            )
        )
        return resp.text.strip()
    except Exception:
        logger.warning("Tool-output summarisation failed for %s - falling back to truncation", component, exc_info=True)
        return ""


async def summarise_tool_messages(
    messages: list[dict],
    indices_to_compact: list[int],
    *,
    inference_router: Any = None,
    component: str = "agent",
    task_id: str | None = None,
) -> int:
    """Replace retired tool results with summaries, falling back to truncation.

    Returns how many were summarised rather than cut.

    Cutting an old result to a head is the weakest thing north does with context:
    measured against summarising the same material it scored 0.66 to 1.00, and
    0.00 on the two 64k tasks, because the answer sits in the middle of a long
    output far more often than at its start. Spilling the remainder to
    `read_tool_output` made that recoverable; it did not make it *read*, because
    recovery depends on the agent choosing to go back for it. A summary is in
    front of the agent either way.

    One model call per result, and each result is summarised at most once ever -
    `_shrinkable_indices` skips anything already carrying a handle - so the cost
    is proportional to the oversized results a task produces, not to how many
    times compaction runs.
    """
    targets = _shrinkable_indices(messages, indices_to_compact)
    summarised = 0
    if targets and inference_router is not None and hasattr(inference_router, "complete"):
        task = _task_text(messages)
        summaries = await asyncio.gather(
            *(
                _summarise_one_result(
                    original=messages[idx]["content"],
                    task=task,
                    tool_name=_tool_name_for(messages, idx),
                    inference_router=inference_router,
                    component=component,
                    task_id=task_id,
                )
                for idx in targets
            )
        )
        for idx, summary in zip(targets, summaries, strict=True):
            original = messages[idx]["content"]
            handle = store_overflow("history", original)
            if summary:
                _replace_with_summary(messages[idx], handle, original, summary)
                summarised += 1
            else:
                _replace_with_head(messages[idx], handle, original)
    else:
        for idx in targets:
            original = messages[idx]["content"]
            _replace_with_head(messages[idx], store_overflow("history", original), original)

    _shrink_paired_assistant_args(messages, indices_to_compact)
    return summarised


def _retired_tool_indices(messages: list[dict], keep_recent: int) -> list[int]:
    """Which tool results are old enough to retire from the window.

    The one place this is decided, so the truncating and summarising paths can
    never disagree about which results are still in play.
    """
    tool_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return tool_indices[:-2] if len(tool_indices) > 2 else []
    return tool_indices[:-keep_recent]


def compact_history(messages: list[dict], keep_recent: int = 4) -> list[dict]:
    """Compact the history by truncating older tool responses to save context.

    Mutates and returns the same list so callers can chain. Also truncates the
    arguments on the paired assistant tool_call so both halves of the exchange
    shrink together - preventing context bloat from large input payloads that
    were already executed.
    """
    _truncate_tool_messages(messages, _retired_tool_indices(messages, keep_recent))
    return messages


async def compact_history_with_summaries(
    messages: list[dict],
    keep_recent: int = 4,
    *,
    inference_router: Any = None,
    component: str = "agent",
    task_id: str | None = None,
) -> int:
    """`compact_history`, but summarising retired results instead of cutting them.

    Same choice of what to retire; a better replacement for what is retired.
    Degrades to exactly `compact_history` when no router is available.
    """
    return await summarise_tool_messages(
        messages,
        _retired_tool_indices(messages, keep_recent),
        inference_router=inference_router,
        component=component,
        task_id=task_id,
    )


async def compact_if_needed(
    messages: list[dict],
    *,
    tokens_in: int = 0,
    model_used: str = "",
    inference_router: Any = None,
    component: str = "agent",
    task_id: str | None = None,
    keep_recent: int = 4,
    max_summary_tokens: int = COMPACT_TOKENS_DEFAULT,
) -> None:
    """LLM-summarise old exchanges when token usage exceeds the compaction threshold.

    Keeps [0] system, [1] user-task, and the last `keep_recent` tool exchanges
    verbatim. Everything in between is replaced with a single summarised block.
    Falls back to truncation-only if the summarisation call fails.
    """
    context_window = context_window_for(model_used, router=inference_router)
    estimated_tokens = estimate_messages_tokens(messages)
    effective_tokens = max(tokens_in, estimated_tokens)

    if effective_tokens < context_window * COMPACTION_THRESHOLD:
        await compact_history_with_summaries(
            messages,
            keep_recent=keep_recent,
            inference_router=inference_router,
            component=component,
            task_id=task_id,
        )
        return

    exchanges = exchange_boundaries(messages)
    if len(exchanges) <= keep_recent:
        await compact_history_with_summaries(
            messages,
            keep_recent=keep_recent,
            inference_router=inference_router,
            component=component,
            task_id=task_id,
        )
        return

    first_kept = exchanges[-keep_recent][0]
    to_summarise = messages[2:first_kept]  # exclude system(0) + user-task(1)
    if not to_summarise:
        return

    history_text = render_exchange_for_summary(to_summarise)
    max_words = int(max_summary_tokens * 0.70)
    prompt = load_prompt("prompts/context_compaction.md").format(max_words=max_words, history_text=history_text)

    if inference_router is not None and hasattr(inference_router, "complete"):
        try:
            resp = await inference_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.LOW,
                    component=f"{component}:compact",
                    task_id=task_id,
                    max_tokens=max_summary_tokens,
                )
            )
            summary = resp.text.strip()
            if summary:
                messages[2:first_kept] = [
                    {"role": "assistant", "content": f"## Earlier context (auto-compacted)\n{summary}"},
                    {
                        "role": "user",
                        "content": "Please proceed with the remaining task requirements using this context.",
                    },
                ]
                return
        except Exception:
            logger.warning(
                "Context compaction summarization failed for %s - falling back to truncation",
                component,
                exc_info=True,
            )

    # Cut rather than summarise: reaching here means the summariser was absent or
    # has just failed on this same router, so asking it again per-result would
    # pay the latency of every call to arrive at the same fallback.
    compact_history(messages, keep_recent=keep_recent)
