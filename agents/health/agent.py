"""Health Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.llm_agent import LLMAgent


class HealthAgent(LLMAgent):
    """Domain specialist for workout plans, calorie/meal tracking, and general fitness."""

    # Note: name and domain are dynamically overridden by __init__ via Agent base class
