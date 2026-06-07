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
COMPACT_TOKENS_DEFAULT = 512   # ~350 words — general agents
COMPACT_TOKENS_HEAVY   = 1000  # ~700 words — agents with bash/git/patch_file
# keep_recent used when context overflows every available model's window.
COMPACT_KEEP_RECENT_OVERFLOW: int = 1
# Max chars per field/line kept when rendering history for summarisation.
_RENDER_PREVIEW_CHARS: int = 200
# Thresholds for truncating large tool outputs during history compaction.
_COMPACT_TRUNCATE_THRESHOLD: int = 500   # skip outputs shorter than this
_COMPACT_TRUNCATE_KEEP: int = 300        # chars kept from oversized outputs

# Ordered from most-specific to least-specific so the first match wins.
# Covers provider-prefixed IDs (e.g. "anthropic/claude-3-haiku") as well as
# bare names.
_CONTEXT_WINDOW_TABLE: tuple[tuple[str, int], ...] = (
    ("gemini-2",      1_000_000),
    ("gemini-1.5",    1_000_000),
    ("gemini",          128_000),
    ("claude",          200_000),
    ("o1",              200_000),
    ("o3",              200_000),
    ("gpt-4o",          128_000),
    ("gpt-4-turbo",     128_000),
    ("gpt-4.1",         128_000),
    ("llama",           128_000),
    ("qwen",            128_000),
    ("mistral",         128_000),
    ("deepseek",        128_000),
    ("phi",              16_000),
)
_DEFAULT_CONTEXT_WINDOW = 128_000


def context_window_for(model: str) -> int:
    """Return the published context-window size (tokens) for a model identifier."""
    m = model.lower()
    for fragment, size in _CONTEXT_WINDOW_TABLE:
        if fragment in m:
            return size
    return _DEFAULT_CONTEXT_WINDOW


def exchange_boundaries(messages: list[dict]) -> list[tuple[int, int]]:
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


def render_exchange_for_summary(messages: list[dict]) -> str:
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
                    args_str = json.dumps(args)[: _RENDER_PREVIEW_CHARS]
                except Exception:
                    args_str = str(fn.get("arguments", ""))[: _RENDER_PREVIEW_CHARS]
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
                lines.append(f"  ← result: {str(content)[: _RENDER_PREVIEW_CHARS]}")
        elif role == "user":
            lines.append(f"[user context: {str(msg.get('content', ''))[: _RENDER_PREVIEW_CHARS]}]")
    return "\n".join(lines)


def compact_history(messages: list[dict], keep_recent: int = 4) -> list[dict]:
    """Compact the history by truncating older tool responses to save context.

    Mutates and returns the same list so callers can chain. Also truncates the
    arguments on the paired assistant tool_call so both halves of the exchange
    shrink together — preventing context bloat from large input payloads that
    were already executed.
    """
    tool_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return

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
    return messages


async def compact_if_needed(
    messages: list[dict],
    *,
    tokens_in: int,
    model_used: str,
    inference_router: Any,
    component: str,
    task_id: str | None,
    keep_recent: int,
    max_summary_tokens: int = COMPACT_TOKENS_DEFAULT,
) -> None:
    """LLM-summarise old exchanges when token usage exceeds the compaction threshold.

    Keeps [0] system, [1] user-task, and the last `keep_recent` tool exchanges
    verbatim. Everything in between is replaced with a single summarised block.
    Falls back to truncation-only if the summarisation call fails.
    """
    context_window = context_window_for(model_used)
    if tokens_in < context_window * COMPACTION_THRESHOLD:
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
        "Preserve: what was accomplished, key facts discovered, file paths, function names, "
        "error messages, and any important data values.\n"
        "Omit: raw file contents, verbose outputs, redundant retries.\n"
        f"Max {max_words} words.\n\n"
        f"<history>\n{history_text}\n</history>"
    )

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
    except Exception:
        logger.warning(
            "Context compaction summarization failed for %s — falling back to truncation",
            component, exc_info=True,
        )
        compact_history(messages, keep_recent=keep_recent)
        return

    messages[2:first_kept] = [
        {"role": "user", "content": f"## Earlier context (auto-compacted)\n{summary}"},
        {"role": "assistant", "content": "Understood — I have the compacted context."},
    ]
