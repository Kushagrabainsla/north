"""Agent module constants."""

from __future__ import annotations

# Supports full researcher→architect→coder↔tester chains with multiple fix cycles.
MAX_DELEGATION_DEPTH = 10

# Engineering agents must be found exactly - no silent fallback to general.
ENGINEERING_AGENTS: frozenset[str] = frozenset({"researcher", "architect", "coder", "tester"})

# Cap JSON-serialised tool results injected back into the conversation.
# ~40k chars ≈ 10k tokens - generous but bounded.
MAX_TOOL_RESULT_CHARS = 40_000
# Minimum chars allocated per field when splitting the cap across a tool result dict.
_TOOL_RESULT_MIN_FIELD_CHARS: int = 200
