"""Exceptions for the skills subsystem."""

from __future__ import annotations


class SkillError(Exception):
    """Base class for skill errors."""


class SkillParseError(SkillError):
    """A SKILL.md file could not be parsed into valid frontmatter + body."""


class SkillNotFoundError(SkillError):
    """No skill is registered under the requested name."""
