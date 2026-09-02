"""Deciding which files are worth spending a model call on.

Discovery ranks candidates on their *names*, which is cheap but nearly blind: it
cannot tell three copies of the same transcript apart, cannot see that a PDF
holds no extractable text, and cannot tell your resume from someone else's.

The survey answers those questions locally, before any model is called. It reads
each candidate once - which extraction has to do anyway, so the text is carried
forward rather than read twice - and drops the ones that cannot pay their way:

* **empty** - no extractable text, so there is nothing to extract from;
* **duplicate** - byte-identical to a file already kept;
* **someone else's** - a personal document that never mentions the user;
* **surplus** - the fifth resume, when two already say everything.

What survives is re-scored on what the file actually *contains* rather than what
it is called. Every drop is recorded with its reason, so the run can report what
it looked at instead of only what it kept.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Below this, a file has no usable prose - an image-only PDF, a stub, a file
# whose text layer did not extract. A model call on it can only waste money.
_MIN_USEFUL_CHARS = 200

# Document families and the words that identify one. A family is what a document
# *is*, independent of which version of it this is, so the seven variants of a
# resume collapse to one family and can be capped as a group. Order matters: the
# first match wins, so put the more specific kinds first.
_FAMILY_KEYWORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("resume", frozenset({"resume", "cv", "curriculum"})),
    ("transcript", frozenset({"transcript", "transcripts", "grades", "marksheet"})),
    ("offer", frozenset({"offer", "contract", "agreement", "lease", "employment"})),
    ("schedule", frozenset({"schedule", "timetable", "calendar", "itinerary", "onboarding"})),
    ("finance", frozenset({"budget", "invoice", "receipt", "statement", "tax", "payslip", "salary"})),
    ("health", frozenset({"health", "medical", "diet", "nutrition", "workout", "fitness", "prescription"})),
    # Immigration and identity paperwork. Grouped and capped hard because these
    # are near-duplicates of each other and consist largely of numbers the
    # extraction prompt is required to never emit.
    ("identity", frozenset({
        "passport", "license", "licence", "visa", "insurance", "ssn", "aadhaar",
        "i9", "i20", "i94", "i589", "ds160", "ead", "opt", "cpt", "sevis", "greencard",
    })),
    ("goals", frozenset({"goals", "plan", "roadmap", "okr", "journal", "reflection"})),
    ("writing", frozenset({"article", "post", "blog", "essay", "draft", "linkedin"})),
    ("project", frozenset({"readme", "spec", "design", "architecture", "findings", "blueprint"})),
)

# How many files of one family are worth reading. Two resumes differ in emphasis
# and are both worth having; the seventh says nothing the first six did not.
_FAMILY_CAP: dict[str, int] = {
    "resume": 2,
    "transcript": 1,
    "offer": 2,
    "schedule": 2,
    "finance": 3,
    "health": 3,
    "identity": 1,
    "goals": 3,
    "writing": 3,
    "project": 3,
}
_DEFAULT_FAMILY_CAP = 3

# Families that describe a specific person. One of these that never mentions the
# user is somebody else's document, and its facts do not belong in their profile.
_PERSONAL_FAMILIES: frozenset[str] = frozenset({"resume", "transcript", "offer", "identity"})

# Content signals worth points, beyond simply mentioning the user.
_CONTENT_SIGNALS: tuple[tuple[str, int], ...] = (
    (r"\b(?:experience|employment|education|skills)\b", 25),
    (r"\b(?:gpa|degree|university|college|semester)\b", 20),
    (r"\b(?:salary|stipend|compensation|rent|budget)\b", 20),
    (r"\b(?:allerg|vegetarian|vegan|diet|workout|training)\w*", 20),
    (r"\b(?:deadline|due|recurring|every\s+(?:mon|tue|wed|thu|fri|sat|sun|week|month))\w*", 15),
)

_USER_MENTION_POINTS = 60
_LENGTH_POINTS_CAP = 40
_CHARS_PER_LENGTH_POINT = 500

# Below this a file carries no personal signal at all - no mention of the user,
# no recognisable subject matter, barely any text. An order confirmation scores
# here; a one-page class schedule does not.
_MIN_USEFUL_SCORE = 25

# Words that say nothing about what a document is, so they must not decide which
# family it belongs to.
_STOPWORD_TOKENS: frozenset[str] = frozenset(
    {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "my", "copy",
     "final", "draft", "latest", "new", "old", "v", "pdf", "doc", "docx", "signed"}
)

# How many meaningful words name an unrecognised family. Two is too coarse
# ("internship report" swallows unrelated documents); four is so specific that
# near-identical names stop matching.
_FALLBACK_FAMILY_TOKENS = 3

# How many files to read for each one kept. Enough that a good file ranked
# poorly by name still gets read and can win on content; bounded so a crowded
# Downloads folder cannot turn a one-time scan into a long one.
_READ_BUDGET_MULTIPLIER = 4


@dataclass(frozen=True)
class SurveyedFile:
    """A candidate that earned a model call, with the text already read."""

    path: Path
    text: str
    content_hash: str
    family: str
    score: int


@dataclass
class SurveyReport:
    """What the survey looked at and what it decided, for honest reporting."""

    considered: int = 0
    kept: list[SurveyedFile] = field(default_factory=list)
    dropped: list[tuple[Path, str]] = field(default_factory=list)

    def drop(self, path: Path, reason: str) -> None:
        self.dropped.append((path, reason))

    @property
    def drop_reasons(self) -> Counter[str]:
        return Counter(reason for _path, reason in self.dropped)

    def summary(self) -> str:
        """One line naming what happened to everything considered."""
        if not self.dropped:
            return f"{len(self.kept)} of {self.considered} files kept"
        breakdown = ", ".join(f"{count} {reason}" for reason, count in self.drop_reasons.most_common())
        return f"{len(self.kept)} of {self.considered} files kept ({breakdown})"


def survey_files(
    candidates: Iterable[Path],
    *,
    read_text: Callable[[Path], str],
    user_tokens: set[str],
    budget: int,
    max_reads: int | None = None,
) -> SurveyReport:
    """Read *candidates* locally and pick the *budget* best worth a model call.

    Reads past the budget on purpose. Stopping at the first *budget* survivors
    would spend the whole allowance on whatever discovery happened to rank first
    and never reach a meal plan sitting further down - which is the same
    name-ordering the survey exists to correct. Reading is bounded by
    *max_reads* so a large disk cannot make this unbounded.
    """
    read_limit = max_reads if max_reads is not None else budget * _READ_BUDGET_MULTIPLIER
    report = SurveyReport()
    seen_hashes: set[str] = set()
    per_family: Counter[str] = Counter()
    reads = 0

    for path in candidates:
        if reads >= read_limit:
            break
        reads += 1
        report.considered += 1

        family = family_of(path, user_tokens)
        cap = _FAMILY_CAP.get(family, _DEFAULT_FAMILY_CAP)
        # A full family normally means "skip without reading". Personal families
        # are the exception: a colleague's resume must be identified as theirs,
        # not merely crowded out by the user's own. Otherwise, with one resume on
        # disk instead of seven, it would be kept as the second one.
        if per_family[family] >= cap and family not in _PERSONAL_FAMILIES:
            report.drop(path, f"surplus {family}")
            continue

        try:
            text = read_text(path)
        except Exception:
            logger.debug("survey: could not read %s", path, exc_info=True)
            report.drop(path, "unreadable")
            continue

        if len(text.strip()) < _MIN_USEFUL_CHARS:
            report.drop(path, "no extractable text")
            continue

        # Ownership before duplication: a colleague's resume must be reported as
        # somebody else's whether or not the user's own resumes happened to fill
        # the family first. Getting the right answer for the wrong reason means
        # the check is one lucky ordering away from letting it through.
        mentions_user = _mentions_user(text, user_tokens)
        if family in _PERSONAL_FAMILIES and user_tokens and not mentions_user:
            report.drop(path, "about someone else")
            continue

        # Deferred until after the ownership check so the cap counts only files
        # that are actually the user's.
        if per_family[family] >= cap:
            report.drop(path, f"surplus {family}")
            continue

        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        if content_hash in seen_hashes:
            report.drop(path, "duplicate")
            continue

        score, signals = _content_score(text, mentions_user=mentions_user)
        # A low score means "nothing here identifies this as personal". Matching a
        # subject signal is exactly that identification, so a short meal plan or
        # workout log stays even though it names nobody and is barely a page.
        if score < _MIN_USEFUL_SCORE and not signals:
            report.drop(path, "too little of substance")
            continue

        seen_hashes.add(content_hash)
        per_family[family] += 1
        report.kept.append(
            SurveyedFile(
                path=path,
                text=text,
                content_hash=content_hash,
                family=family,
                score=score,
            )
        )

    report.kept.sort(key=lambda surveyed: -surveyed.score)
    for surplus in report.kept[budget:]:
        report.drop(surplus.path, "outranked")
    del report.kept[budget:]
    return report


def family_of(path: Path, user_tokens: set[str] | None = None) -> str:
    """Which kind of document this is, judged from its name.

    An unrecognised document falls back to the first few meaningful words of its
    name, so that "GPU Checkpoint - Findings" and "GPU Checkpoint - Final Report"
    land in one family and cap against each other. Using the whole stem instead
    would give every variant its own family, which is the same as no cap at all.
    """
    ordered = [token for token in re.split(r"[^a-z0-9]+", path.stem.lower()) if token]
    # Also match across a separator, so "I-20", "I 94" and "DS 160" are recognised
    # as the identity documents they are rather than as the letter "i".
    joined = {ordered[i] + ordered[i + 1] for i in range(len(ordered) - 1)}
    tokens = set(ordered) | joined
    for family, keywords in _FAMILY_KEYWORDS:
        if tokens & keywords:
            return family

    skip = _STOPWORD_TOKENS | (user_tokens or set())
    # Single letters are fragments of a split name ("I-20" -> "i", "20"), not a
    # description of anything, and would group unrelated documents together.
    meaningful = [token for token in ordered if len(token) > 1 and token not in skip and not token.isdigit()]
    if not meaningful:
        return path.stem.lower().strip() or "other"
    return " ".join(meaningful[:_FALLBACK_FAMILY_TOKENS])


def _mentions_user(text: str, user_tokens: set[str]) -> bool:
    """True when the document names the user. Cheap proxy for "this is about me"."""
    if not user_tokens:
        return False
    head = text[:20_000].lower()
    return any(token in head for token in user_tokens if len(token) >= 3)


def _content_score(text: str, *, mentions_user: bool) -> tuple[int, int]:
    """Rank a file on what it contains, which is what its name could only guess at.

    Returns the score and how many subject signals matched. The count matters on
    its own: a short file that names nobody still earns its place if it is
    recognisably about the user's health, money, or schedule.
    """
    score = _USER_MENTION_POINTS if mentions_user else 0
    score += min(len(text) // _CHARS_PER_LENGTH_POINT, _LENGTH_POINTS_CAP)
    body = text[:20_000].lower()
    signals = 0
    for pattern, points in _CONTENT_SIGNALS:
        if re.search(pattern, body):
            score += points
            signals += 1
    return score, signals
