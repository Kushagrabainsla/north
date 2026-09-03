"""Context compaction utilities for the agent ReAct loop.

Responsible for keeping the conversation history within the model's context
window by summarising old tool-call exchanges via the LLM or falling back to
simple truncation when summarisation is unavailable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from inference.models import CompletionRequest, PoolPriority

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


def _truncate_tool_messages(messages: list[dict], indices_to_compact: list[int]) -> None:
    call_id_to_assistant: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id")
                if cid:
                    call_id_to_assistant[cid] = i

    compacted_assistant: set[int] = set()

    for idx in indices_to_compact:
        msg = messages[idx]
        content = msg.get("content")
        if isinstance(content, str) and len(content) > _COMPACT_TRUNCATE_THRESHOLD:
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
                msg["content"] = content[:_COMPACT_TRUNCATE_KEEP] + "... [Large tool output truncated to save context]"

        call_id = msg.get("tool_call_id")
        if call_id and call_id in call_id_to_assistant:
            ast_idx = call_id_to_assistant[call_id]
            if ast_idx not in compacted_assistant:
                ast_msg = messages[ast_idx]
                for tc in ast_msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str) and len(args) > _RENDER_PREVIEW_CHARS:
                        fn["arguments"] = "{}"
                compacted_assistant.add(ast_idx)


def compact_history(messages: list[dict], keep_recent: int = 4) -> list[dict]:
    """Compact the history by truncating older tool responses to save context.

    Mutates and returns the same list so callers can chain. Also truncates the
    arguments on the paired assistant tool_call so both halves of the exchange
    shrink together - preventing context bloat from large input payloads that
    were already executed.
    """
    tool_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        if len(tool_indices) > 2:
            _truncate_tool_messages(messages, tool_indices[:-2])
        return messages

    to_compact = tool_indices[:-keep_recent]
    _truncate_tool_messages(messages, to_compact)
    return messages


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
        compact_history(messages, keep_recent=keep_recent)
        return

    exchanges = exchange_boundaries(messages)
    if len(exchanges) <= keep_recent:
        compact_history(messages, keep_recent=keep_recent)
        return

    first_kept = exchanges[-keep_recent][0]
    to_summarise = messages[2:first_kept]  # exclude system(0) + user-task(1)
    if not to_summarise:
        return

    history_text = render_exchange_for_summary(to_summarise)
    max_words = int(max_summary_tokens * 0.70)
    prompt = (
        "You are summarising intermediate steps of an ongoing AI agent task.\n"
        "Condense the following tool calls and results into a concise bullet-point summary.\n"
        "Preserve: what was accomplished, the exact list of files created or modified, key facts "
        "discovered, file paths, function names, the most recent error or failing test, important data "
        "values, and what still remains to be done.\n"
        "Omit: raw file contents, verbose outputs, redundant retries.\n"
        f"Max {max_words} words.\n\n"
        f"<history>\n{history_text}\n</history>"
    )

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

    compact_history(messages, keep_recent=keep_recent)

