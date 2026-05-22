"""University Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.llm_agent import LLMAgent


class UniversityAgent(LLMAgent):
    """Domain specialist for academic duties, courses, assignments, and study plans."""

    # Note: name and domain are dynamically overridden by __init__ via Agent base class
