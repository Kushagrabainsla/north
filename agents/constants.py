"""Agent module constants."""

from __future__ import annotations

# Supports full researcher→architect→coder↔reviewer chains with multiple fix cycles.
MAX_DELEGATION_DEPTH = 10

# Engineering agents must be found exactly - no silent fallback to general.
ENGINEERING_AGENTS: frozenset[str] = frozenset({"researcher", "architect", "coder", "reviewer"})

# Engineering agents that never touch production code. Their `write_file` calls
# produce handoff documents - research notes, specs - which is the whole of their
# job, so the code-evidence gate must not read one as an unverified code change.
# Every research task was being told "modified code but ran no check_types or
# test to verify the change" for writing its own context.md.
NO_CODE_AGENTS: frozenset[str] = frozenset({"researcher", "architect"})

# Tools that change code, and the tools that check it. A run that used the first
# without the second changed code no one verified - see orchestrator/result_audit.py.
CODE_MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file", "rename_symbol"})
CODE_VERIFY_TOOLS: frozenset[str] = frozenset({"check_types", "bash", "lint"})

# Cap JSON-serialised tool results injected back into the conversation.
# ~40k chars ≈ 10k tokens - generous but bounded.
MAX_TOOL_RESULT_CHARS = 40_000
# Minimum chars allocated per field when splitting the cap across a tool result dict.
_TOOL_RESULT_MIN_FIELD_CHARS: int = 200
