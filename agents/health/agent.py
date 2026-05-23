"""Health Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class HealthAgent(AgenticLLMAgent):
    """Domain specialist for workout plans, calorie/meal tracking, and general fitness."""

