"""Researcher Agent - engineering domain context gatherer.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class ResearcherAgent(AgenticLLMAgent):
    """Engineering specialist: gathers context, prior art, and unknowns before design begins."""
