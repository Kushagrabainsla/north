"""Retiring a learned skill that keeps being present when tasks go wrong.

north distils skills from its own runs. That is the point of it - but it means a
skill can encode a mistake, and nothing ever looked back. One learned skill told
every future agent to read a handoff file and *stop* if it was missing; the
agents followed it, the tasks failed, and the skill stayed active. A prompt fix
was being overruled by a procedure north had written for itself.

So the loop is closed here: a learned skill that has been selected for enough
tasks, most of which ended badly, is marked ``retired``. The registry already
refuses to offer a retired skill (``Skill.applies_to``), so nothing else has to
change. Retiring is reversible by editing the file - this never deletes.

Only LEARNED skills are considered. A hand-authored skill that shows up in
failures is evidence about the tasks, not about the skill.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import yaml

from skills.models import SkillSource

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"

# Terminal task actions that mean the run did not do what was asked.
BAD_OUTCOMES: frozenset[str] = frozenset({"task_failed", "task_completed_with_failures"})

# A skill has to have been tried this many times before its record means
# anything - two bad runs out of two is a coincidence, not a pattern.
MIN_USES: int = 3
# ...and this share of those runs must have gone badly.
FAILURE_RATIO: float = 0.6


def skills_to_retire(
    selections: Iterable[tuple[str, Iterable[str]]],
    outcomes: dict[str, str],
    *,
    min_uses: int = MIN_USES,
    failure_ratio: float = FAILURE_RATIO,
) -> list[str]:
    """Which skills have a bad enough record to retire.

    *selections* is ``(task_id, skill_names)`` per selection event; *outcomes*
    maps a task id to its terminal ledger action. Tasks with no recorded outcome
    are ignored rather than counted either way - a run still in flight is not
    evidence, and treating it as a success would hide a skill that is failing
    right now.

    Pure, so the policy can be tested without a ledger or a filesystem.
    """
    tallies: dict[str, list[int]] = {}
    for task_id, names in selections:
        outcome = outcomes.get(task_id)
        if outcome is None:
            continue
        for name in names:
            tally = tallies.setdefault(name, [0, 0])
            tally[0] += 1
            if outcome in BAD_OUTCOMES:
                tally[1] += 1
    return sorted(
        name
        for name, (uses, bad) in tallies.items()
        if uses >= min_uses and bad / uses >= failure_ratio
    )


def retire(learned_dir: Path, names: Iterable[str]) -> list[str]:
    """Mark each named learned skill ``retired`` on disk; return those changed.

    Rewrites only the ``status`` field, leaving the body and provenance intact so
    the record of what north believed - and why it was withdrawn - survives.
    A skill that is already retired, missing, or unreadable is skipped quietly:
    this runs on a schedule and must never be the reason a cleanup pass fails.
    """
    changed: list[str] = []
    for name in names:
        path = learned_dir / name / SKILL_FILENAME
        try:
            document = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = _with_retired_status(document)
        if updated is None or updated == document:
            continue
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError:
            logger.warning("Could not retire learned skill %r", name, exc_info=True)
            continue
        changed.append(name)
        logger.info("Retired learned skill %r - it was selected for tasks that kept failing", name)
    return changed


def _with_retired_status(document: str) -> str | None:
    """Return *document* with ``status: retired``, or None if it is not a skill.

    The frontmatter is re-serialised with yaml rather than patched by hand: a
    description containing ``: `` or ``#`` would make string surgery produce a
    file that no longer parses, and an unparseable skill is one that silently
    stops loading.
    """
    if not document.startswith("---"):
        return None
    parts = document.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    if str(frontmatter.get("source") or "") != SkillSource.LEARNED.value:
        return None  # never touch a hand-authored skill
    if str(frontmatter.get("status") or "active") == "retired":
        return document
    frontmatter["status"] = "retired"
    serialised = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{serialised}---{parts[2]}"


async def sweep(ledger, learned_dir: Path, *, days: int = 30) -> list[str]:
    """Read the recent ledger, retire the learned skills with a bad record.

    Best-effort: this runs from a scheduled cleanup job, so a query that fails
    must not take the rest of the sweep down with it.
    """
    from datetime import UTC, datetime, timedelta

    from ledger.base import LedgerFilters

    try:
        entries = await ledger.query(
            LedgerFilters(since=datetime.now(UTC) - timedelta(days=days), limit=20_000)
        )
    except Exception:
        logger.warning("Skill retirement: could not read the ledger", exc_info=True)
        return []

    selections: list[tuple[str, list[str]]] = []
    outcomes: dict[str, str] = {}
    for entry in entries:
        task_id = getattr(entry, "task_id", None)
        action = str(getattr(entry, "action", "") or "")
        if not task_id:
            continue
        if action == "skill_selected":
            names = [n.strip() for n in str(getattr(entry, "output", "") or "").split(",") if n.strip()]
            if names:
                selections.append((task_id, names))
        elif action.startswith("task_completed") or action in BAD_OUTCOMES:
            # The ledger is newest-first, and a task has exactly one terminal
            # action, so the first one seen is the one that counts.
            outcomes.setdefault(task_id, action)

    return retire(learned_dir, skills_to_retire(selections, outcomes))
