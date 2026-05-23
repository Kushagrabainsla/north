"""GeneralAgent — catch-all for conversational and cross-domain requests.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.llm_agent import LLMAgent


class GeneralAgent(LLMAgent):
    """Handles conversation, planning, Q&A, and anything not claimed by a domain agent."""
