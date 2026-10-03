"""Asking the other coding agent to find what is wrong with an agent's change.

The reviewer is a second agent, read-only, working on the diff. Its verdict is advice shown to the user and
to north's memory decider; it never lands or blocks a change by itself. The diff is the author's own text,
so it is handed over fenced as untrusted data.
"""

from __future__ import annotations

import re

from coding_agents.models import Review, ReviewVerdict
from utils.prompts import load_prompt

MAX_DIFF_CHARS = 60_000
MAX_SUMMARY_CHARS = 2_000
_VERDICT = re.compile(r"^\s*VERDICT:\s*(OK|CONCERNS)\b", re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r"<<<\s*(?:BEGIN|END)\s+UNTRUSTED\s+DIFF\s*>>>", re.IGNORECASE)


def review_guidance() -> str:
    """The reviewer's standing instructions."""
    return load_prompt("prompts/coding_agent_review.md")


def review_task(task: str, diff: str) -> str:
    """What the reviewer is asked: the task the author had, and the author's change, fenced as data."""
    body = _FENCE.sub("", diff)  # a diff cannot close its own fence
    note = ""
    if len(body) > MAX_DIFF_CHARS:
        body, note = body[:MAX_DIFF_CHARS], "\n[the diff was cut here: read the rest of the files in this copy]"
    return (
        f"The task the author was given:\n{task}\n\n"
        f"The author's change, as a diff:\n<<<BEGIN UNTRUSTED DIFF>>>\n{body}{note}\n<<<END UNTRUSTED DIFF>>>\n\n"
        "Review it now."
    )


def parse_review(reviewer: str, text: str) -> Review:
    """The reviewer's verdict and what it said. No readable verdict is UNCLEAR, never a pass."""
    found = _VERDICT.search(text)
    if found is None:
        return Review(reviewer, ReviewVerdict.UNCLEAR, text.strip()[:MAX_SUMMARY_CHARS])
    verdict = ReviewVerdict.OK if found.group(1).upper() == "OK" else ReviewVerdict.CONCERNS
    return Review(reviewer, verdict, (text[: found.start()] + text[found.end() :]).strip()[:MAX_SUMMARY_CHARS])
