"""Parse a SKILL.md document into its frontmatter fields and markdown body."""

from __future__ import annotations

import re

import yaml

from skills.exceptions import SkillParseError

# A SKILL.md is `---\n<yaml frontmatter>\n---\n<markdown body>`.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def parse_skill_document(text: str) -> tuple[dict, str]:
    """Return ``(frontmatter_dict, body)`` for a SKILL.md document.

    Raises SkillParseError when the frontmatter is present but not a valid YAML
    mapping. A document with no frontmatter yields an empty dict and the whole
    text as the body, letting the registry reject it for the missing fields.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text.strip()

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillParseError(f"invalid YAML frontmatter: {exc}") from exc

    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise SkillParseError(f"frontmatter must be a mapping, got {type(frontmatter).__name__}")

    return frontmatter, match.group(2).strip()
