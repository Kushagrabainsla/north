"""Small deterministic intent predicates shared across orchestration stages.

North reads a task's intent from its words, with no model call: a planner model
once classified every task first, which put about 4 seconds in front of every
answer to pick the one agent there is.
"""

from __future__ import annotations

import re

from utils.text import extract_markdown_section

# Only the recent end of the dialogue says what a short follow-up refers to.
_CONVERSATION_TAIL_CHARS = 2000
# Asks that get the goal check: sending, applying, buying or deleting something,
# or creating north's own flows, skills and tools. Merely using a signed-in browser
# profile is not here: the browser asks before using a profile on each site.
_CONSEQUENTIAL_PATTERNS = (
    re.compile(
        r"\b(?:apply|applying|submit|send)\b[^.\n]{0,100}\b(?:job|application|form|message|email)\b"
        r"|\b(?:job|application|form|message|email)\b[^.\n]{0,100}\b(?:apply|submit|send)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:create|build|add|set\s*up|activate|schedule|enable|update)\b[^.\n]{0,100}"
        r"\b(?:north\s+)?(?:flow|skill|tool|automation)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:buy|purchase|sell|transfer|pay|delete|remove permanently)\b", re.IGNORECASE),
)
_DEEP_WORK_TERMS = re.compile(
    r"\b(audit|benchmark|compare|debug|design|fix|implement|investigate|modify|performance|research|security|test|trace|verify)\b",
    re.IGNORECASE,
)
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


def is_consequential(prompt: str, context: str = "") -> bool:
    """Whether the ask acts outside north, so it is checked against your goals.

    The session's goal and recent dialogue count, so "yes, go ahead" after "send
    the application" is caught; the rest of *context* (facts about you) does not,
    or a fact that mentions deleting something would flag every task.
    """
    sections = [extract_markdown_section(context, name) for name in ("Active session goal", "Recent conversation")]
    text = "\n".join([*(section[-_CONVERSATION_TAIL_CHARS:] for section in sections if section), prompt])
    return any(pattern.search(text) for pattern in _CONSEQUENTIAL_PATTERNS)


def execution_profile(prompt: str, workspace: str) -> str:
    """``quick_readonly`` for a plain overview of the chosen repository, else ``standard``.

    The quick profile gives a read-only budget; asking for deep work (fix, debug,
    research...) keeps the full one.
    """
    overview = bool(workspace) and is_repository_overview(prompt)
    return "quick_readonly" if overview and not _DEEP_WORK_TERMS.search(prompt) else "standard"
