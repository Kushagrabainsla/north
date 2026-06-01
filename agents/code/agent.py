"""CodeAgent — agentic specialist for reading, writing, and running code.

DEPRECATED: Should not be used. Will be removed once the specialised
Researcher/Architect/Coder/Tester agents are in place.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class CodeAgent(AgenticLLMAgent):
    """Reads, writes, searches, and executes code inside a workspace directory.

    DEPRECATED — do not route new tasks here.
    """
