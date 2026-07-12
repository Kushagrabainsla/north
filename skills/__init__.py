"""Skills: reusable procedural knowledge injected into engineering agents.

A skill is a folder with a ``SKILL.md`` (YAML frontmatter + markdown body). The
right skill for a task is selected semantically and injected into the agent's
context *before* it acts, so the same model makes fewer avoidable mistakes.
Built-in skills ship in the repo; learned skills are distilled from north's own
successful task history at runtime.
"""

from __future__ import annotations

from skills.models import SKILL_FILENAME, Skill, SkillSource
from skills.registry import SkillRegistry
from skills.selector import SkillSelector

__all__ = ["SKILL_FILENAME", "Skill", "SkillRegistry", "SkillSelector", "SkillSource"]
