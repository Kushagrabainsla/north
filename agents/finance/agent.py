"""Finance Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.llm_agent import LLMAgent


class FinanceAgent(LLMAgent):
    """Domain specialist for budget planning, expenditures logging, and market data insights."""

    # Note: name and domain are dynamically overridden by __init__ via Agent base class
