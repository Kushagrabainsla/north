"""Coder Agent — engineering domain implementer.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class CoderAgent(AgenticLLMAgent):
    """Engineering specialist: implements code against the architect's spec."""
