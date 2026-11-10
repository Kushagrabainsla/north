"""Agent module constants."""

from __future__ import annotations

# North is the one agent: every task, flow step and schedule runs on it.
NORTH_AGENT = "north"

# Cap JSON-serialised tool results injected back into the conversation.
# ~40k chars ≈ 10k tokens - generous but bounded.
MAX_TOOL_RESULT_CHARS = 40_000
# Minimum chars allocated per field when splitting the cap across a tool result dict.
_TOOL_RESULT_MIN_FIELD_CHARS: int = 200
