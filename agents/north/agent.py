"""NorthAgent - the one agent: every task, flow step and schedule runs on it.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class NorthAgent(AgenticLLMAgent):
    """North itself: conversation, planning, research, your devices, and handing coding to your coding agents."""
