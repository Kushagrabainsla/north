"""Job Agent domain specialist.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class JobAgent(AgenticLLMAgent):
    """Domain specialist for career management, resumes, applications, and networking drafts."""
