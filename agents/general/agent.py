"""GeneralAgent — catch-all for conversational and cross-domain requests.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class GeneralAgent(AgenticLLMAgent):
    """Handles conversation, planning, Q&A, and anything not claimed by a domain agent."""
