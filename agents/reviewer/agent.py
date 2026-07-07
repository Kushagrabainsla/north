"""Reviewer Agent - engineering domain quality gate.

The reviewer is north's QA + code-review specialist: it runs the test suite,
reviews the coder's diff for bugs, quality, and security issues, and reports a
concrete fix-list back to the coder. See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

from agents.agentic_llm_agent import AgenticLLMAgent


class ReviewerAgent(AgenticLLMAgent):
    """Engineering specialist: quality gate - runs tests, reviews the diff, reports fixes."""
