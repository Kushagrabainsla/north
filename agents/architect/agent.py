"""Architect Agent - engineering domain design specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class ArchitectAgent(AgenticLLMAgent):
    """Engineering specialist: owns design decisions, produces the spec."""
