"""Value objects for the skills subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Every skill lives in its own folder as this file.
SKILL_FILENAME = "SKILL.md"


class SkillSource(StrEnum):
    """Where a skill came from."""

    BUILTIN = "builtin"  # hand-authored, ships in the repo
    LEARNED = "learned"  # distilled at runtime from north's successful episodes


@dataclass(frozen=True)
class Skill:
    """One reusable procedure an engineering agent can be given.

    ``description`` is the retrieval key and must be written as a trigger ("Use
    when ...") so semantic selection matches it to the right tasks. ``body`` is
    the procedural markdown injected into the agent's context on selection.
    """

    name: str
    description: str
    body: str
    directory: Path
    source: SkillSource = SkillSource.BUILTIN
    # Used to attribute outcomes to the exact procedure that was injected.
    version: str = "1.0.0"
    status: str = "active"
    # Task ids a learned skill was distilled from (empty for built-in skills).
    provenance: tuple[str, ...] = ()
    # Agent domains this skill may be injected into. Defaults to engineering (where
    # nearly all skills belong); a skill for another domain (e.g. the literature-review
    # skill, which serves the general assistant) declares the domains it serves.
    domains: frozenset[str] = frozenset({"engineering"})

    def metadata_line(self) -> str:
        """One-line ``name: description`` used in the compact fallback listing."""
        return f"- {self.name}: {self.description}"

    def available_to(self, domain: str) -> bool:
        """True when this skill may be injected into an agent of ``domain``."""
        return self.status == "active" and domain in self.domains

    def bundled_file_names(self) -> list[str]:
        """Names of supporting files in the skill folder (everything but SKILL.md).

        These are read-only references the agent may open with ``read_file``;
        the skill system never executes them.
        """
        if not self.directory.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.directory.iterdir()
            if entry.is_file() and entry.name != SKILL_FILENAME
        )
