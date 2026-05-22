"""Job Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.llm_agent import LLMAgent


class JobAgent(LLMAgent):
    """Domain specialist for career management, resumes, applications, and networking drafts."""

    # Note: name and domain are dynamically overridden by __init__ via Agent base class
