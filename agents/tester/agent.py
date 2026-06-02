"""Tester Agent — engineering domain QA specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class TesterAgent(AgenticLLMAgent):
    """Engineering specialist: QA — writes tests, runs them, verifies correctness."""
