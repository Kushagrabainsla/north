"""Small deterministic intent predicates shared across orchestration stages."""

from __future__ import annotations

import re

_REPOSITORY_TERMS = re.compile(r"\b(repo|repository|codebase|project)\b", re.IGNORECASE)
_OVERVIEW_TERMS = re.compile(
    r"\b(overview|summary|summarize|describe|explain|context|what is|what does|"
    r"walk me through|structure|architecture|map)\b",
    re.IGNORECASE,
)
_HOW_IT_WORKS = re.compile(
    r"\bhow\s+(?:does\s+)?[^\n]{0,80}\bworks?\b|"
    r"\bhow\s+is\s+[^\n]{0,80}\b(?:structured|organized|built)\b",
    re.IGNORECASE,
)


def is_repository_overview(prompt: str, *, repository_context: bool = False) -> bool:
    """Whether *prompt* asks for broad understanding of a repository.

    ``repository_context`` is true when the caller already knows the task is an
    engineering workspace request. It lets terse follow-ups such as "give me an
    overview of what it is" retain the same evidence requirement without making
    ordinary conversational summaries look like repository work.
    """
    has_overview_language = bool(_OVERVIEW_TERMS.search(prompt) or _HOW_IT_WORKS.search(prompt))
    return has_overview_language and (repository_context or bool(_REPOSITORY_TERMS.search(prompt)))
