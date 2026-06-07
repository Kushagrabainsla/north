"""News Briefing Agent — daily digest of current events across four topic areas."""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class NewsBriefingAgent(AgenticLLMAgent):
    """Compiles a daily news digest from live web searches across Tech & AI,
    world events, science & health, and business & markets."""
