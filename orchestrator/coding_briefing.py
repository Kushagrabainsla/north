"""What north tells a coding agent before it starts: the user's facts, profile and matching skills.

The agent cannot know "always use type hints" or "this repo is mine to push to" unless it is told, and it
is told in the prompt, the way a repository's own instruction file is. Nothing here grants anything: only
the approval layer decides what the agent may do, so a wrong or poisoned fact can mislead but not authorize.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

COMPONENT = "coding_agent"
DOMAIN = "engineering"
_FACTS = 8
_SKILLS = 2
_FACT_CHARS = 300
_PROFILE_CHARS = 1500
_SKILL_CHARS = 1800
_QUERY_CHARS = 500
_FENCE = re.compile(r"<<<\s*(?:BEGIN|END)[^>]*>>>", re.IGNORECASE)


class MemoryBriefing:
    """Recalls the user's facts and profile for a task, and the skills that match it."""

    def __init__(self, memory: Any, skills: Any = None) -> None:
        self._memory = memory
        self._skills = skills

    async def brief(self, task: str, workspace: str) -> str:
        """A short block of context, or an empty string. Never raises: a briefing must not stop a run."""
        parts: list[str] = []
        try:
            parts += await self._known(task, workspace)
        except Exception:
            logger.warning("could not recall the user's memory for a coding run", exc_info=True)
        try:
            parts += await self._skill_notes(task)
        except Exception:
            logger.warning("could not select skills for a coding run", exc_info=True)
        return "\n\n".join(parts)

    async def _known(self, task: str, workspace: str) -> list[str]:
        principal = await self._memory.principal_for(COMPONENT, domain=DOMAIN, workspace=workspace)
        # Episodes are left out: they are what past tasks produced, and the least trustworthy thing memory holds.
        recalled = await self._memory.recall(principal, task[:_QUERY_CHARS], fact_limit=_FACTS, episode_limit=0)
        parts = []
        if recalled.facts:
            lines = "\n".join(f"- {_clean(fact)[:_FACT_CHARS]}" for fact in recalled.facts[:_FACTS])
            parts.append(f"Facts the user has stated:\n{lines}")
        for document in recalled.documents:
            parts.append(f"The user's profile:\n{_clean(document)[:_PROFILE_CHARS]}")
        return parts

    async def _skill_notes(self, task: str) -> list[str]:
        if self._skills is None:
            return []
        chosen = await self._skills.select(task)
        return [
            f"A procedure that fits this kind of task ({skill.name}):\n{_clean(skill.body)[:_SKILL_CHARS]}"
            for skill in chosen[:_SKILLS]
        ]


def _clean(text: str) -> str:
    """Strip anything shaped like a fence, so remembered text cannot pose as the end of its own section."""
    return _FENCE.sub("", str(text)).strip()
