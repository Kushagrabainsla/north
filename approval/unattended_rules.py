"""The safe-action list, as data you own rather than a tuple in a source file.

`approval/unattended.py` used to *be* this list: a tuple of test runners and a
frozenset of git verbs, shipped in Python. Two things were wrong with that. It
was entirely engineering-shaped, so nothing outside coding could ever be
automated. And it was not yours to change - adding one entry meant editing
Python and restarting, you could not see the effective list, and you could not
remove an entry you disagreed with.

The second is the more serious one, and `approval_memory` next door already
states the principle: *a decision that cannot be withdrawn is not consent*. A
hardcoded allowlist fails that test outright.

So the rules live here, in `approval_memory.db` beside the learned decisions -
same lifecycle, both answering "what may north do without asking me" - and the
web UI edits them.

Shipped rules are seeded as ordinary rows you can edit, disable or restore, the
way built-in schedules already work. Disabling one is remembered, so an upgrade
does not quietly resurrect a rule you turned off; a rule shipped later still
appears, because seeding is tracked per rule rather than by a single "seeded"
flag.

What is *not* here: the two hard rules. Sending something to another human and
spending money are never auto-approvable, and they are enforced in code in
`unattended.py`, after this table is consulted. As rows they would be one
careless write away from being off, silently and one-way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.db import open_db_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unattended_rules (
    id         TEXT     NOT NULL PRIMARY KEY,
    kind       TEXT     NOT NULL,
    pattern    TEXT     NOT NULL,
    enabled    INTEGER  NOT NULL DEFAULT 1,
    source     TEXT     NOT NULL DEFAULT 'user',
    note       TEXT     NOT NULL DEFAULT '',
    fire_count INTEGER  NOT NULL DEFAULT 0,
    last_fired_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

_SEEDED_SCHEMA = """
CREATE TABLE IF NOT EXISTS unattended_rules_seeded (
    id TEXT PRIMARY KEY
)
"""

# The kinds a rule can be about. `read` is deliberately absent: an action that
# changes nothing is already allowed in every mode by the policy's first tier,
# so a list of read rules would be a second answer to a settled question.
KIND_COMMAND = "command"
KIND_GIT = "git"
KIND_DEVICE = "device"
KIND_SELF_MESSAGE = "self_message"
KINDS: tuple[str, ...] = (KIND_COMMAND, KIND_GIT, KIND_DEVICE, KIND_SELF_MESSAGE)

# What north ships with. Matched as a whole first token or a two-token prefix;
# a bare interpreter (e.g. `python -c ...`) is deliberately not included.
_BUILTIN_COMMANDS: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "go test",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "cargo test",
    "ruff check",
    "ruff format",
    "mypy",
    "tsc",
    "npx tsc",
    "go vet",
    "go build",
    "make test",
)

# Local, reversible, workspace-bound. Network (push/pull) and merge never.
_BUILTIN_GIT: tuple[str, ...] = (
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "checkout",
    "add",
    "commit",
    "stash",
)

# Trivially reversible physical actions, where the undo is another toggle. An
# explicit verb list, never a general "smart home" category: "unlock" is a
# toggle by shape, and must not be reachable by describing it as one.
_BUILTIN_DEVICE: tuple[str, ...] = ("on", "off", "toggle", "set_brightness")

# Messages addressed to the operator themselves - a digest, a reminder, an
# alert. Never a message to anyone else; see the hard rules in unattended.py.
_BUILTIN_SELF_MESSAGE: tuple[str, ...] = ("notify_user", "send_self_message")

_BUILTINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_COMMAND, _BUILTIN_COMMANDS),
    (KIND_GIT, _BUILTIN_GIT),
    (KIND_DEVICE, _BUILTIN_DEVICE),
    (KIND_SELF_MESSAGE, _BUILTIN_SELF_MESSAGE),
)


def rule_id(kind: str, pattern: str) -> str:
    """A rule's identity, derived so a shipped rule keeps its id across installs.

    Seeding is tracked by this id, which is what lets a rule you disabled stay
    disabled through an upgrade while a rule shipped *later* still appears.
    """
    return f"{kind}:{pattern}"


@dataclass(frozen=True)
class UnattendedRule:
    """One entry in the safe-action list."""

    id: str
    kind: str
    pattern: str
    enabled: bool
    source: str  # 'builtin' (shipped, restorable) or 'user' (yours)
    note: str
    fire_count: int
    last_fired_at: str | None
    created_at: str
    updated_at: str

    @property
    def is_builtin(self) -> bool:
        return self.source == "builtin"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pattern": self.pattern,
            "enabled": self.enabled,
            "source": self.source,
            "note": self.note,
            "fire_count": self.fire_count,
            "last_fired_at": self.last_fired_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_rule(row: Any) -> UnattendedRule:
    return UnattendedRule(
        id=row["id"],
        kind=row["kind"],
        pattern=row["pattern"],
        enabled=bool(row["enabled"]),
        source=row["source"],
        note=row["note"],
        fire_count=row["fire_count"],
        last_fired_at=row["last_fired_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class UnattendedRuleStore:
    """CRUD over the safe-action list, cached for the approval hot path."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.execute(_SEEDED_SCHEMA)
            self._seed(conn)
        self._cache: dict[str, tuple[str, ...]] | None = None

    # ── Seeding ──────────────────────────────────────────────────────────────

    @staticmethod
    def _seed(conn: Any) -> None:
        """Insert shipped rules that have never been seeded on this install.

        Keyed per rule, not by one global flag. A single flag would mean a rule
        added in a later version never appearing for anyone who had already run
        the old one - the failure mode where a security default silently does
        not apply to existing users.
        """
        seeded = {r["id"] for r in conn.execute("SELECT id FROM unattended_rules_seeded").fetchall()}
        now = _now()
        for kind, patterns in _BUILTINS:
            for pattern in patterns:
                rid = rule_id(kind, pattern)
                if rid in seeded:
                    continue  # already shipped once; if it is gone, you removed it
                conn.execute(
                    "INSERT OR IGNORE INTO unattended_rules "
                    "(id, kind, pattern, enabled, source, note, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'builtin', '', ?, ?)",
                    (rid, kind, pattern, now, now),
                )
                conn.execute("INSERT OR IGNORE INTO unattended_rules_seeded (id) VALUES (?)", (rid,))

    @staticmethod
    def shipped_patterns(kind: str) -> tuple[str, ...]:
        """What north ships for *kind*, for restoring a built-in rule."""
        for builtin_kind, patterns in _BUILTINS:
            if builtin_kind == kind:
                return patterns
        return ()

    # ── Read ─────────────────────────────────────────────────────────────────

    def _ensure_cache(self) -> dict[str, tuple[str, ...]]:
        """kind -> enabled patterns. Read on every approval, so it is cached."""
        if self._cache is None:
            by_kind: dict[str, list[str]] = {kind: [] for kind in KINDS}
            for rule in self.all():
                if rule.enabled:
                    by_kind.setdefault(rule.kind, []).append(rule.pattern)
            self._cache = {kind: tuple(patterns) for kind, patterns in by_kind.items()}
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    def patterns(self, kind: str) -> tuple[str, ...]:
        """The enabled patterns for *kind*, as the policy needs them."""
        return self._ensure_cache().get(kind, ())

    def all(self) -> list[UnattendedRule]:
        """Every rule, enabled or not, grouped by kind then pattern."""
        try:
            with open_db_connection(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM unattended_rules ORDER BY kind, pattern").fetchall()
        except Exception:
            logger.warning("UnattendedRuleStore: failed to read rules", exc_info=True)
            return []
        return [_row_to_rule(row) for row in rows]

    def get(self, rule_identifier: str) -> UnattendedRule | None:
        with open_db_connection(self._db_path) as conn:
            row = conn.execute("SELECT * FROM unattended_rules WHERE id = ?", (rule_identifier,)).fetchone()
        return _row_to_rule(row) if row else None

    # ── Write ────────────────────────────────────────────────────────────────

    def add(self, kind: str, pattern: str, note: str = "") -> UnattendedRule:
        """Add one rule of your own. Raises ValueError on an unknown kind."""
        if kind not in KINDS:
            raise ValueError(f"unknown rule kind: {kind!r} (expected one of {', '.join(KINDS)})")
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("a rule needs a pattern")
        now = _now()
        rid = rule_id(kind, pattern)
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO unattended_rules (id, kind, pattern, enabled, source, note, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 'user', ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET enabled = 1, note = excluded.note, updated_at = excluded.updated_at",
                (rid, kind, pattern, note, now, now),
            )
        self._invalidate()
        got = self.get(rid)
        assert got is not None  # just written
        return got

    def update(
        self, rule_identifier: str, *, enabled: bool | None = None, note: str | None = None
    ) -> UnattendedRule | None:
        """Enable, disable, or annotate a rule.

        A rule's pattern is not editable in place: the id is derived from it, and
        an edited pattern is a different rule. The UI deletes and adds, which
        also keeps the fire count honest rather than carrying it to a rule that
        never earned it.
        """
        existing = self.get(rule_identifier)
        if existing is None:
            return None
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE unattended_rules SET enabled = ?, note = ?, updated_at = ? WHERE id = ?",
                (
                    int(existing.enabled if enabled is None else enabled),
                    existing.note if note is None else note,
                    _now(),
                    rule_identifier,
                ),
            )
        self._invalidate()
        return self.get(rule_identifier)

    def delete(self, rule_identifier: str) -> bool:
        """Remove a rule. A shipped rule is disabled instead of deleted.

        Deleting a built-in outright would let the next upgrade's seeding decide
        whether it comes back, which is not a decision an upgrade should be
        making about something you removed.
        """
        existing = self.get(rule_identifier)
        if existing is None:
            return False
        if existing.is_builtin:
            self.update(rule_identifier, enabled=False)
            return True
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM unattended_rules WHERE id = ?", (rule_identifier,))
        self._invalidate()
        return True

    def restore_builtins(self) -> int:
        """Re-enable every shipped rule, re-adding any that were deleted.

        The "restore defaults" the built-in schedules page already offers.
        Returns how many rules changed.
        """
        changed = 0
        now = _now()
        with open_db_connection(self._db_path) as conn:
            for kind, patterns in _BUILTINS:
                for pattern in patterns:
                    rid = rule_id(kind, pattern)
                    cursor = conn.execute(
                        "INSERT INTO unattended_rules "
                        "(id, kind, pattern, enabled, source, note, created_at, updated_at) "
                        "VALUES (?, ?, ?, 1, 'builtin', '', ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET enabled = 1, updated_at = excluded.updated_at "
                        "WHERE unattended_rules.enabled = 0",
                        (rid, kind, pattern, now, now),
                    )
                    changed += cursor.rowcount or 0
        self._invalidate()
        return changed

    def record_fire(self, kind: str, pattern: str) -> None:
        """Note that a rule auto-approved something.

        A rule that has fired 40 times is a different object from one that has
        never fired, and the difference is exactly what you want to see before
        deciding whether to keep it. Best-effort: failing to count must never
        fail the approval it is counting.
        """
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "UPDATE unattended_rules SET fire_count = fire_count + 1, last_fired_at = ? WHERE id = ?",
                    (_now(), rule_id(kind, pattern)),
                )
        except Exception:
            logger.debug("UnattendedRuleStore: could not record a rule firing", exc_info=True)
