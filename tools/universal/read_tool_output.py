"""ReadToolOutputTool - read back the part of a tool result that did not fit.

An oversized tool result is capped before it reaches the model, and the rest is
kept in the overflow store rather than thrown away. This tool is how an agent
reaches that remainder: search it for what it actually wants, or page through
it from an offset.

Without this, truncation silently deleted the middle of every long output - the
grep match halfway down, the value in the body of a response - and the agent
answered from the head as though nothing was missing.
"""

from __future__ import annotations

import re
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.output_spill import DEFAULT_READ_CHARS, MAX_READ_CHARS, spill_store


class ReadToolOutputTool(Tool):
    """Read or search the full text of a tool result that was truncated."""

    name = "read_tool_output"
    description = (
        "Read the part of an earlier tool result that was too long to show. "
        "When a result says characters were 'not shown' and gives a handle, the rest is NOT gone - "
        "it is kept under that handle, and this tool reads it. "
        "action='find' searches the full output with a regular expression and returns the matching "
        "sections with surrounding context - use this first, it is far cheaper than paging. "
        "action='read' returns a window of the raw text from 'offset', for reading on in order. "
        "Use this whenever the answer you need might have been in the omitted part."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The handle from the truncation note, e.g. 'tool_output:ab12cd34'.",
            },
            "action": {
                "type": "string",
                "enum": ["find", "read"],
                "description": (
                    "find = search the full output with a regex and return matches with context (preferred); "
                    "read = return raw text from 'offset' onwards. Default: find when a pattern is given, else read."
                ),
            },
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for. Required for action='find'.",
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to read from, for action='read'. Default 0.",
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Characters to return for action='read'. Default {DEFAULT_READ_CHARS}, max {MAX_READ_CHARS}."
                ),
            },
        },
        "required": ["handle"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        if data.get("action") == "find":
            matches = data.get("matches", [])
            if not matches:
                return (
                    f"No match for {data.get('pattern')!r} in the full output "
                    f"({data.get('total_chars')} chars). The pattern genuinely does not appear - "
                    "this is a complete search, not a truncated one."
                )
            lines = [f"{len(matches)} match(es) in the full output ({data['total_chars']} chars):"]
            for match in matches:
                lines.append(f"\n--- at offset {match['offset']} ---\n{match['excerpt']}")
            return "\n".join(lines)

        parts = [data.get("text", "")]
        next_offset = data.get("next_offset")
        if next_offset is not None:
            parts.append(
                f"\n\n[{data['returned_chars']} chars from offset {data['offset']} of {data['total_chars']} total. "
                f"Continue with offset={next_offset}.]"
            )
        else:
            parts.append(f"\n\n[End of output - {data['total_chars']} chars total.]")
        return "".join(parts)

    async def run(self, input: ToolInput) -> ToolOutput:
        handle = (input.params.get("handle") or "").strip()
        if not handle:
            return ToolOutput(success=False, error="Parameter 'handle' is required.")

        pattern = (input.params.get("pattern") or "").strip()
        action = (input.params.get("action") or ("find" if pattern else "read")).strip()
        store = spill_store()

        if action == "find":
            if not pattern:
                return ToolOutput(success=False, error="Parameter 'pattern' is required for action='find'.")
            try:
                matches = store.find(handle, pattern)
            except re.error as exc:
                return ToolOutput(success=False, error=f"Invalid regular expression {pattern!r}: {exc}")
            if matches is None:
                return _expired(handle)
            probe = store.read(handle, offset=0, limit=1)
            return ToolOutput(
                success=True,
                data={
                    "action": "find",
                    "handle": handle,
                    "pattern": pattern,
                    "total_chars": probe.total_chars if probe else 0,
                    "matches": [{"offset": m.offset, "excerpt": m.excerpt} for m in matches],
                },
            )

        if action == "read":
            offset = _as_int(input.params.get("offset"), 0)
            limit = _as_int(input.params.get("limit"), DEFAULT_READ_CHARS)
            window = store.read(handle, offset=offset, limit=limit)
            if window is None:
                return _expired(handle)
            return ToolOutput(
                success=True,
                data={
                    "action": "read",
                    "handle": handle,
                    "tool_name": window.tool_name,
                    "text": window.text,
                    "offset": window.offset,
                    "returned_chars": window.returned_chars,
                    "total_chars": window.total_chars,
                    "next_offset": window.next_offset,
                },
            )

        return ToolOutput(success=False, error=f"Unknown action {action!r}. Use 'find' or 'read'.")


def _as_int(raw: Any, default: int) -> int:
    """Read a numeric parameter leniently - models send '0' as often as 0."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _expired(handle: str) -> ToolOutput:
    """The overflow store is bounded, so a handle can legitimately be gone."""
    return ToolOutput(
        success=False,
        failure_kind="refused",
        error=(
            f"No stored output for handle {handle!r}. The overflow store is bounded and this entry has been "
            "evicted, or the handle is wrong. Re-run the original tool to regenerate it - and narrow the "
            "request (a more specific path, pattern, or range) so the result fits this time."
        ),
    )
