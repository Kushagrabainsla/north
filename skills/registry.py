"""SkillRegistry - discover skills from the built-in (repo) and learned directories.

Mirrors AgentRegistry: it scans each source directory for ``<name>/SKILL.md``
folders and constructs a Skill per valid one. A malformed, oversized, or
incomplete skill is skipped with a warning rather than crashing every agent - a
bad skill must never take the whole system down.
"""

from __future__ import annotations

import logging
from pathlib import Path

from skills.exceptions import SkillNotFoundError, SkillParseError
from skills.models import SKILL_FILENAME, Skill, SkillSource
from skills.parser import parse_skill_document

logger = logging.getLogger(__name__)

# A skill body longer than this is rejected: skills are injected into context, so
# an oversized one would blow the budget and degrade the model rather than help it.
MAX_BODY_CHARS = 8_000


def rejection_reason(name: str, description: str, body: str) -> str:
    """Return why a skill is invalid, or "" when it is valid.

    Shared by the registry (loading skills) and the distiller (before writing a
    learned skill) so both apply exactly the same acceptance bar.
    """
    if not name:
        return "missing name"
    if not description:
        return "missing description"
    if not body:
        return "empty body"
    if len(body) > MAX_BODY_CHARS:
        return f"body exceeds {MAX_BODY_CHARS} chars"
    return ""


class SkillRegistry:
    """Loads skills from a built-in directory and an optional learned directory.

    Built-in skills are loaded first, so a learned skill can never shadow a
    hand-authored one of the same name.
    """

    def __init__(self, builtin_dir: Path, learned_dir: Path | None = None) -> None:
        self._sources: list[tuple[Path, SkillSource]] = [(builtin_dir, SkillSource.BUILTIN)]
        if learned_dir is not None:
            self._sources.append((learned_dir, SkillSource.LEARNED))
        self._skills: dict[str, Skill] = {}
        self._discover()

    def _discover(self) -> None:
        self._skills = {}
        for directory, source in self._sources:
            self._load_directory(directory, source)

    def _load_directory(self, directory: Path, source: SkillSource) -> None:
        if not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            skill_file = entry / SKILL_FILENAME
            if not (entry.is_dir() and skill_file.is_file()):
                continue
            skill = self._load_skill(entry, skill_file, source)
            if skill is None:
                continue
            if skill.name in self._skills and source is SkillSource.LEARNED:
                continue  # a learned skill never overrides a built-in of the same name
            self._skills[skill.name] = skill

    def _load_skill(self, directory: Path, skill_file: Path, source: SkillSource) -> Skill | None:
        try:
            frontmatter, body = parse_skill_document(skill_file.read_text(encoding="utf-8"))
        except (SkillParseError, OSError) as exc:
            logger.warning("SkillRegistry: skipping %s - %s", directory.name, exc)
            return None

        name = str(frontmatter.get("name") or directory.name).strip()
        description = str(frontmatter.get("description") or "").strip()
        reason = rejection_reason(name, description, body)
        if reason:
            logger.warning("SkillRegistry: skipping skill %r - %s", name or directory.name, reason)
            return None

        provenance = tuple(str(t) for t in (frontmatter.get("provenance") or []))
        raw_domains = frontmatter.get("domains")
        domains = (
            frozenset(str(d).strip() for d in raw_domains if str(d).strip())
            if isinstance(raw_domains, list) and raw_domains
            else frozenset({"engineering"})
        )
        return Skill(
            name=name,
            description=description,
            body=body,
            directory=directory,
            source=source,
            provenance=provenance,
            domains=domains,
        )

    def reload(self) -> None:
        """Re-scan the source directories (e.g. after the distiller writes a skill)."""
        self._discover()

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise SkillNotFoundError(f"No skill registered with name: {name}")
        return self._skills[name]

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return list(self._skills.keys())
