"""Async first-run bootstrap: scans user files, extracts facts, seeds memory.

Runs once as a background task after startup. Never blocks the user's first
prompt. Safe to cancel at any point — already-stored facts survive and the
bootstrapped marker is written only on clean completion.

File selection: Prioritizes high-ROI personal files (resumes, CVs, budgets,
personal notes, schedules, goals) across Downloads, Documents, Desktop, and Notes
directories. Excludes code repositories, build outputs, and noisy lab dumps.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import pwd
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from bootstrap.schema import (
    UNIFIED_EXTRACTION_JSON_SCHEMA,
    FactTopic,
    UserProfile,
)
from bootstrap.survey import SurveyedFile, survey_files
from inference.base import InferenceRouter
from inference.exceptions import AllModelsRateLimitedError
from inference.models import CompletionRequest, PoolPriority
from memory.base import ContextStore
from memory.facts import FactStore
from memory.models import ContextDocument
from utils.prompts import load_prompt
from utils.text import extract_json

logger = logging.getLogger(__name__)

_BOOTSTRAPPED_MARKER = ".bootstrapped"
_PROGRESS_FILE = ".bootstrap_progress.json"
_BOOTSTRAP_VERSION = 3
_MAX_FILES = 25
# Candidates handed to the survey, which reads each one and keeps the best
# _MAX_FILES. Names alone are a weak signal, so it needs room to discard.
_CANDIDATE_POOL = _MAX_FILES * 4
_SOURCE_QUOTAS = {
    "documents": 15,
    "desktop": 10,
    "downloads": 10,
    "notes": 10,
    "project_readmes": 2,
    "home_root": 5,
}
_MAX_SOURCE_FILE_BYTES = 15 * 1024 * 1024
_MAX_EXTRACTED_TEXT_CHARS = 100_000
# Room for a filled-in profile across eight sections plus up to fifteen facts.
# The old 2,500 was routinely spent before the answer began.
_EXTRACTION_MAX_TOKENS = 4_000
_MAX_DOCUMENT_PAGES = 30
_BOOTSTRAP_DELAY_SECONDS = 2.0
_SOURCE_DIRS = ("Downloads", "Documents", "Desktop")

# Tokenized keyword dictionaries for word-boundary matching
_TIER1_KEYWORDS = frozenset(
    {
        "resume",
        "cv",
        "bio",
        "profile",
        "portfolio",
        "coverletter",
        "about",
        "aboutme",
        "application",
        "vitae",
    }
)

_TIER2_KEYWORDS = frozenset(
    {
        "goals",
        "goal",
        "habits",
        "habit",
        "routine",
        "routines",
        "health",
        "fitness",
        "workout",
        "diet",
        "nutrition",
        "budget",
        "budgets",
        "finances",
        "finance",
        "expenses",
        "expense",
        "spending",
        "tax",
        "taxes",
        "journal",
        "diary",
        "preferences",
        "preference",
        "personal",
        "schedule",
        "schedules",
        "todo",
        "todos",
    }
)

_TIER3_KEYWORDS = frozenset(
    {
        "transcript",
        "offer",
        "contract",
        "coursework",
        "summary",
        "notes",
        "note",
        "eval",
        "review",
    }
)

_NOISE_KEYWORDS = frozenset(
    {
        "readme",
        "license",
        "licence",
        "changelog",
        "contributing",
        "api",
        "spec",
        "architecture",
        "docker",
        "dockerfile",
        "dataset",
        "datasets",
        "dump",
        "dumps",
        "export",
        "exports",
        "log",
        "logs",
        "sample",
        "samples",
        "assignment",
        "assignments",
        "lab",
        "labs",
        "homework",
        "syllabus",
        "fixture",
        "fixtures",
        "test",
        "tests",
        "benchmark",
        "benchmarks",
    }
)

# Backward compatibility alias
_BOOST_FILENAMES = _TIER1_KEYWORDS | _TIER2_KEYWORDS
_DEPRIORITIZE_FILENAMES = _NOISE_KEYWORDS

_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".config",
        ".ssh",
        ".aws",
        ".cache",
        ".npm",
        ".cargo",
        ".rustup",
        ".local",
        ".Trash",
        "build",
        "dist",
    }
)

_EXTENSIONS = frozenset(
    {
        ".csv",
        ".txt",
        ".md",
        ".json",
        ".yaml",
        ".yml",
        ".pdf",
        ".docx",
    }
)

# Path fragments that indicate low-value / noisy files
_DENY_PATH_FRAGMENTS = (
    "lab0",
    "lab1",
    "lab2",
    "lab3",
    "lab4",
    "lab5",
    "lab6",
    "lab7",
    "lab8",
    "lab9",
    "lab10",
    "lab11",
    "lab12",
    "keys",
    "key",
    "priv",
    "complex",
    "system_prompts_leaks",
    "node_modules",
    ".git",
    "__pycache__",
)

# Baseline density weight by extension: equal value for primary doc types
_EXT_DENSITY = {
    ".pdf": 50,
    ".docx": 50,
    ".md": 50,
    ".txt": 50,
    ".csv": 50,
    ".json": 30,
    ".yaml": 30,
    ".yml": 30,
}


def _get_user_tokens() -> set[str]:
    """Name tokens identifying the primary user, from the system account.

    The account's full name is the valuable one: a login of ``jsmith`` appears in
    no document, while "John Smith" appears in every one that is actually about
    them. That is what lets the survey tell the user's resume from a colleague's.
    """
    tokens: set[str] = set()

    def _add(raw: str) -> None:
        for part in re.split(r"[-_\s.0-9]+", raw.lower()):
            if len(part) >= 2:
                tokens.add(part)

    for source in (_account_full_name, getpass.getuser, lambda: Path.home().name):
        try:
            value = source()
        except Exception:
            continue
        if value:
            _add(value)
    return tokens


def _account_full_name() -> str:
    """The account holder's real name, when the OS records one (GECOS on Unix)."""
    return pwd.getpwuid(os.getuid()).pw_gecos.split(",", 1)[0].strip()


def _tokenize_stem(stem: str) -> set[str]:
    """Tokenize a filename stem into normalized words on punctuation and digit boundaries."""
    return {t for t in re.split(r"[-_\s.0-9]+", stem.lower()) if t}


def _normalize_stem_cluster(stem: str) -> str:
    """Normalize a filename stem to a cluster key by stripping versions and dates."""
    s = stem.lower()
    s = re.sub(r"[-_](?:v\d+|\d{4}|final|latest|copy|draft)\b", "", s)
    s = re.sub(r"[-_\s\d]+$", "", s)
    return s.strip("-_ ") or stem.lower()


def _is_projects_dir(path: Path, home: Path) -> bool:
    """True if path is the user's ~/Desktop/projects directory."""
    projects = (home / "Desktop" / "projects").resolve()
    return path.resolve() == projects


def _is_under_north_repo(path: Path, home: Path) -> bool:
    """True if path is inside north's own checkout."""
    repo = (home / "Desktop" / "projects" / "north").resolve()
    resolved = path.resolve()
    return resolved == repo or repo in resolved.parents


# What a filename is worth before size, age and depth are taken into account.
_USER_NAME_BOOST = 200
_KEYWORD_BOOSTS: tuple[tuple[frozenset[str], int], ...] = (
    (_TIER1_KEYWORDS, 150),  # career and identity
    (_TIER2_KEYWORDS, 100),  # personal lifestyle and operations
    (_TIER3_KEYWORDS, 50),  # secondary notes and summaries
    (_NOISE_KEYWORDS, -150),  # technical noise
)
_DEFAULT_EXT_DENSITY = 20
# A file this well rated is worth reading wherever it was found.
_HIGH_BOOST = 100
_PERSONAL_SOURCE_GROUPS = ("documents", "desktop", "downloads", "notes")
# Size sweetspot: a stub says nothing, and a huge file is a database, not a note.
_STUB_SIZE_BYTES = 100
_STUB_PENALTY = -30
_OVERSIZE_PENALTIES: tuple[tuple[int, int], ...] = ((500_000, -30), (2_000_000, -60), (5_000_000, -100))
_RECENCY_BOOSTS: tuple[tuple[float, int], ...] = ((30.0, 30), (90.0, 15), (365.0, 0))
_STALE_PENALTY = -10


def _keyword_boost(stem_tokens: set[str], user_tokens: set[str]) -> int:
    """What the words in a filename say about how personal the file is."""
    # A file named after the user (Kushagra_Bainsla.pdf) is the strongest signal.
    named_after_user = _USER_NAME_BOOST if stem_tokens & user_tokens else 0
    return named_after_user + sum(weight for keywords, weight in _KEYWORD_BOOSTS if stem_tokens & keywords)


def _size_boost(size: int) -> int:
    penalty = _STUB_PENALTY if size < _STUB_SIZE_BYTES else 0
    return penalty + sum(points for threshold, points in _OVERSIZE_PENALTIES if size > threshold)


def _recency_boost(mtime: float) -> int:
    """Newer files win. An unreadable mtime scores neutral rather than stale."""
    if not mtime:
        return 0
    age_days = (datetime.now().timestamp() - mtime) / 86400.0
    for max_age, points in _RECENCY_BOOSTS:
        if age_days <= max_age:
            return points
    return _STALE_PENALTY


def _depth_below_home(path: Path) -> int:
    try:
        return len(path.relative_to(Path.home()).parts)
    except Exception:
        return len(path.parts)


def _priority_group(boost: int, source_group: str) -> int:
    """Which band this file is picked from; 0 is picked first.

    A well-rated file wins from any personal source (Downloads, Documents,
    Desktop, Notes); everything else waits its turn.
    """
    if boost >= _HIGH_BOOST:
        return 0
    if source_group in _PERSONAL_SOURCE_GROUPS and boost >= 0:
        return 1
    return 2


def _rank_file(
    path: Path,
    source_group: str,
    user_tokens: set[str] | None = None,
) -> tuple[int, int, int, float]:
    """Rank a file for selection priority using multi-factor ROI scoring.

    Returns tuple: (priority_group, -boost_score, depth, -mtime)
    Lower priority_group = higher priority.
    Higher boost_score = higher priority.
    Higher mtime = newer = higher priority.
    """
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0

    boost = (
        _keyword_boost(_tokenize_stem(path.stem), user_tokens if user_tokens is not None else _get_user_tokens())
        + _EXT_DENSITY.get(path.suffix.lower(), _DEFAULT_EXT_DENSITY)
        + _size_boost(size)
        + _recency_boost(mtime)
    )
    return (_priority_group(boost, source_group), -boost, _depth_below_home(path), -mtime)


def _is_safe_path(path: Path, allowed_roots: list[Path]) -> bool:
    """Check if a resolved path is within allowed roots and not a symlink escape."""
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        return False

    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def _get_source_group(path: Path, home: Path) -> str:
    """Determine source group for a given path."""
    try:
        rel = path.relative_to(home).parts
        if not rel:
            return "home_root"
        top = rel[0].lower()
        if top == "documents":
            return "documents"
        if top == "desktop":
            if len(rel) > 1 and rel[1].lower() == "projects":
                return "project_readmes"
            return "desktop"
        if top == "downloads":
            return "downloads"
        if "notes" in top or "obsidian" in top:
            return "notes"
        return "home_root"
    except Exception:
        return "documents"


_DEFAULT_README_QUOTA = 2


def _unique_by_resolved_path(paths: list[Path]) -> list[Path]:
    """First occurrence of each real file, so two links to it are read once."""
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


class _CandidateScan:
    """Collects the personal files bootstrap might read, then ranks them.

    Directories are walked to a depth chosen per source: deep enough to find a
    filed-away document, shallow enough not to wander into a code tree.
    """

    def __init__(self) -> None:
        self.home = Path.home()
        self._allowed_roots = [
            self.home / "Documents",
            self.home / "Desktop",
            self.home / "Downloads",
            self.home / "Notes",
            self.home / "Documents" / "Notes",
            self.home / "Obsidian",
            self.home,  # for flat csv/txt
            self.home / "Desktop" / "projects",  # for READMEs
        ]
        self._found: list[Path] = []

    def walk(self, root: Path, max_depth: int, depth: int = 1) -> None:
        """Collect matching files under root, going no deeper than max_depth."""
        if not root.is_dir() or depth > max_depth:
            return
        try:
            entries = list(root.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if self._is_ignored(entry):
                continue
            if entry.is_dir():
                if self._is_allowed(entry):
                    self.walk(entry, max_depth, depth + 1)
            elif self._is_wanted_file(entry):
                self._found.append(entry)

    def add_loose_home_files(self) -> None:
        """csv/txt sitting directly in the home directory, one level deep only."""
        for pattern in ("*.csv", "*.txt"):
            for path in self.home.glob(pattern):
                if path.is_file() and self._is_wanted_file(path):
                    self._found.append(path)

    def add_project_readmes(self) -> None:
        quota = _SOURCE_QUOTAS.get("project_readmes", _DEFAULT_README_QUOTA)
        taken = 0
        for path in sorted((self.home / "Desktop" / "projects").glob("*/README.md")):
            if taken >= quota:
                return
            if not _is_under_north_repo(path, self.home) and self._is_wanted_file(path):
                self._found.append(path)
                taken += 1

    def pool(self) -> list[Path]:
        """The best candidates found, ranked.

        A pool rather than the final selection. Names alone cannot tell three
        copies of a transcript apart, spot a PDF with no text layer, or notice
        that a resume belongs to somebody else - the survey reads each file and
        decides that (bootstrap/survey.py). Handing it several times the budget
        is what gives it something to choose between.
        """
        user_tokens = _get_user_tokens()
        ranked = sorted(
            _unique_by_resolved_path(self._found),
            key=lambda path: _rank_file(path, _get_source_group(path, self.home), user_tokens=user_tokens),
        )
        return ranked[:_CANDIDATE_POOL]

    def _is_ignored(self, entry: Path) -> bool:
        name = entry.name
        if name.startswith(".") or name in _SKIP_DIRS:
            return True
        if any(fragment in name.lower() for fragment in _DENY_PATH_FRAGMENTS):
            return True
        return _is_under_north_repo(entry, self.home) or _is_projects_dir(entry, self.home)

    def _is_allowed(self, path: Path) -> bool:
        return _is_safe_path(path, self._allowed_roots)

    def _is_wanted_file(self, entry: Path) -> bool:
        """A readable personal document, small enough to be worth extracting."""
        if entry.suffix.lower() not in _EXTENSIONS:
            return False
        try:
            if entry.stat().st_size > _MAX_SOURCE_FILE_BYTES:
                return False
        except OSError:
            return False
        return entry.is_file() and self._is_allowed(entry)


def _discover_files() -> list[Path]:
    """Walk user directories for high-ROI personal text and document files.

    Uses depth bounds per directory, tokenized semantic ranking, and stem-cluster
    deduplication to pick the top _MAX_FILES.
    """
    scan = _CandidateScan()
    scan.walk(scan.home / "Documents", max_depth=3)
    scan.walk(scan.home / "Desktop", max_depth=2)  # _is_projects_dir keeps this out of code
    scan.walk(scan.home / "Downloads", max_depth=2)
    for notes_dir in (scan.home / "Notes", scan.home / "Documents" / "Notes", scan.home / "Obsidian"):
        scan.walk(notes_dir, max_depth=2)
    scan.add_loose_home_files()
    scan.add_project_readmes()
    return scan.pool()


def _read_text(path: Path) -> str:
    """Extract plain text from a file — supports plain text, PDF, and DOCX.

    Falls back to empty string when the required library is not installed
    (gracefully skips the file instead of crashing).
    Respects MAX_DOCUMENT_PAGES and MAX_EXTRACTED_TEXT_CHARS limits.
    """
    suffix = path.suffix.lower()

    if suffix in (".pdf",):
        try:
            from pypdf import PdfReader  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("bootstrap: pypdf not installed — skipping %s", path.name)
            return ""
        try:
            reader = PdfReader(str(path))
            pages = reader.pages[:_MAX_DOCUMENT_PAGES]
            return "\n".join(page.extract_text() or "" for page in pages)[:_MAX_EXTRACTED_TEXT_CHARS]
        except Exception as exc:
            # Expected while surveying: encrypted, scanned, or malformed PDFs are
            # simply files with no text to read. The survey drops them and says
            # so, so a stack trace per file would be noise, not information.
            logger.debug("bootstrap: no text extractable from PDF %s (%s)", path.name, type(exc).__name__)
            return ""

    if suffix in (".docx",):
        try:
            from docx import Document
        except ImportError:
            logger.warning("bootstrap: python-docx not installed — skipping %s", path.name)
            return ""
        try:
            doc = Document(str(path))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text[:_MAX_EXTRACTED_TEXT_CHARS]
        except Exception:
            logger.warning("bootstrap: failed to extract text from DOCX %s", path.name, exc_info=True)
            return ""

    # Plain text
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:_MAX_EXTRACTED_TEXT_CHARS]
    except Exception:
        logger.warning("bootstrap: failed to read %s", path.name, exc_info=True)
        return ""


def _parse_json_lenient(raw: str) -> dict:
    """Parse JSON from model output, tolerating wrapped/prose responses."""
    if not raw:
        raise ValueError("empty model output")
    try:
        return json.loads(raw.strip())
    except (ValueError, TypeError):
        return extract_json(raw)


async def _extract_unified(
    path: Path,
    router: InferenceRouter,
    content: str | None = None,
) -> tuple[list[dict], str]:
    """Single-pass extraction: returns atomic fact candidates and domain markdown summary.

    *content* is the already-read file text when the survey has read it, which is
    the normal path; it is read here only when this is called on its own.
    """
    if content is None:
        content = _read_text(path)
    if not content.strip():
        return [], ""

    prompt = load_prompt("prompts/bootstrap_extraction.md").format(content=content)
    req = CompletionRequest(
        prompt=prompt,
        # Reading a document and returning a filled-in schema is comprehension
        # work, not bulk throughput. On LOW this went to whatever was cheapest -
        # a 2.6B model that answered a schema-enforced request with prose - and
        # the whole run stored one fact from 25 files.
        priority=PoolPriority.MEDIUM,
        component="bootstrap",
        max_tokens=_EXTRACTION_MAX_TOKENS,
        temperature=0.1,
        response_schema=UNIFIED_EXTRACTION_JSON_SCHEMA,
    )
    try:
        resp = await router.complete(req)
    except AllModelsRateLimitedError:
        raise
    except Exception as exc:
        logger.warning("bootstrap: completion failed for %s — %r", path.name, exc)
        return [], ""

    try:
        parsed = _parse_json_lenient(resp.text)
    except Exception:
        logger.warning(
            "bootstrap: LLM returned invalid structured output for %s — %r",
            path.name,
            getattr(resp, "text", "")[:200],
        )
        return [], ""

    # 1. Parse atomic facts
    fact_items = parsed.get("facts", []) if isinstance(parsed, dict) else []
    candidates: list[dict] = []
    seen_facts: set[str] = set()
    # Every candidate records the same source fingerprint, and hashing reads the
    # whole file - so do it once here rather than once per extracted fact.
    source_hash, source_mtime = await asyncio.to_thread(_fingerprint, path)
    source_path = str(path)

    for item in fact_items:
        if isinstance(item, dict):
            if item.get("subject") != "user":
                continue
            text = _clean_fact(item.get("content", ""))
            if text and text not in seen_facts:
                seen_facts.add(text)
                candidates.append(
                    {
                        "content": text,
                        "subject": "user",
                        "topic": _valid_topic(item.get("topic")),
                        "confidence": float(item.get("confidence", 0.8)),
                        "source_path": source_path,
                        "source_hash": source_hash,
                        "source_mtime": source_mtime,
                        "evidence": item.get("evidence"),
                    }
                )

    # 2. Parse profile sections (supports nested {"profile": {...}} and top-level profile dicts)
    raw_profile = parsed.get("profile") if isinstance(parsed, dict) else None
    if (
        not isinstance(raw_profile, dict)
        and isinstance(parsed, dict)
        and any(k in parsed for k in UserProfile.model_fields)
    ):
        raw_profile = parsed

    if isinstance(raw_profile, dict):
        profile = UserProfile(**{k: _profile_strings(raw_profile.get(k)) for k in UserProfile.model_fields})
        sections: list[str] = []
        for section, items in profile.model_dump().items():
            if not items:
                continue
            sections.append(f"## {section.capitalize()}\n" + "\n".join(f"- {i}" for i in items))
            for item in items:
                text = _clean_fact(item)
                if text and text not in seen_facts:
                    seen_facts.add(text)
                    candidates.append(
                        {
                            "content": text,
                            "subject": "user",
                            # The section this came from *is* the topic - the model
                            # already sorted it, so labelling costs nothing here.
                            "topic": _valid_topic(section),
                            "confidence": 0.85,
                            "source_path": source_path,
                            "source_hash": source_hash,
                            "source_mtime": source_mtime,
                            "evidence": None,
                        }
                    )
        profile_md = "\n\n".join(sections)
    else:
        profile_md = ""

    return candidates, profile_md


async def _extract_facts(path: Path, router: InferenceRouter) -> list[dict]:
    """Read path, prompt LLM for facts, return list of FactCandidates."""
    cands, _ = await _extract_unified(path, router)
    return cands


async def _extract_profile(path: Path, router: InferenceRouter) -> tuple[list[dict], str]:
    """Extract a structured user profile from path."""
    cands, md = await _extract_unified(path, router)
    return cands, md


def _synthesize_profile(profile_sections: list[str]) -> str:
    """Merge per-file profile markdown into one consolidated user profile."""
    if not profile_sections:
        return ""
    joined = "\n\n".join(s for s in profile_sections if s)
    cap = 4000
    if len(joined) > cap:
        joined = joined[:cap] + "\n\n...(truncated)"
    return f"# User Profile (synthesized from local files)\n\n{joined}"


def _file_hash(path: Path) -> str:
    """SHA-256 hash of file contents."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _fingerprint(path: Path) -> tuple[str, float]:
    """(sha256, mtime) for one file - the pair the progress checkpoint stores.

    Reads the whole file, so call via to_thread and cache the result per path.
    """
    try:
        return _file_hash(path), path.stat().st_mtime
    except OSError:
        return "", 0.0


def _profile_strings(raw: object) -> list[str]:
    """Coerce one raw profile section into the list[str] UserProfile requires.

    Models routinely return richer entries than the schema asks for - e.g.
    ``{"name": "...", "status": "active"}`` or ``{"content": "...",
    "confidence": 0.9}``. Passing those straight to ``UserProfile`` raised a
    ValidationError that discarded the whole document, so each entry is reduced
    to its text field here and anything unusable is dropped individually.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("content") or item.get("name") or item.get("fact") or item.get("text")
        if item is None:
            continue
        text = str(item).strip()
        if text:
            values.append(text)
    return values


_VALID_TOPICS: frozenset[str] = frozenset(t.value for t in FactTopic)


def _valid_topic(raw: object) -> str:
    """Coerce a model-supplied topic to a known one, defaulting to 'other'.

    The topic becomes the fact's category, and the category decides which tasks
    may read it - so an unrecognised value must land somewhere harmless rather
    than creating a category nothing knows to ask for.
    """
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if candidate in _VALID_TOPICS:
            return candidate
    return FactTopic.OTHER.value


def _clean_fact(fact: object) -> str | None:
    """Normalize one LLM extraction result into a plain fact string."""
    if isinstance(fact, dict):
        fact = fact.get("fact") or fact.get("content") or fact.get("text")
    if fact is None:
        return None
    text = str(fact).strip()
    if not text or not _is_usable_fact(text):
        return None
    return text


_ABSENCE_RE = re.compile(
    r"\b(?:not|no|never|nothing|n't)\b.{0,30}"
    r"\b(?:mention|state|provid|availab|inform|explicit|discuss|record|found|known|indicat|list)\w*",
    re.IGNORECASE,
)

_PII_RE = re.compile(
    r"\b(?:i-?94|passport|ssn|social security|visa number|account number|admission record number)\b",
    re.IGNORECASE,
)

_SECRET_RE = re.compile(
    r"""(?ix)
    (?:^|[\s\W])
    (?:
        (?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token)
        |(?:password|passwd|pwd)
        |(?:private[_-]?key|ssh[_-]?key)
        |(?:aws[_-]?access[_-]?key|aws[_-]?secret[_-]?key)
        |(?:github[_-]?token|gh[_-]?token|ghp_)
        |(?:slack[_-]?token|xox[baprs]-)
        |(?:stripe[_-]?key|sk_live_|pk_live_)
        |(?:jwt[_-]?token|eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*)
        |(?:credit[_-]?card|cc[_-]?num)
        |(?:seed[_-]?phrase|mnemonic)
    )
    [\s:=]+
    [A-Za-z0-9_\-+/=]{8,}
    """,
)

_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_USER_REF_RE = re.compile(
    r"\b(?:user|kushagra|bainsla|he|his|him|she|her|their|they|employee)\b",
    re.IGNORECASE,
)


def _is_usable_fact(text: str) -> bool:
    if not _USER_REF_RE.search(text):
        return False
    return not (_ABSENCE_RE.search(text) or _PII_RE.search(text) or _SECRET_RE.search(text) or _CC_RE.search(text))


def _load_progress(north_home: Path) -> list[dict] | None:
    """Return completed file progress from the progress checkpoint, or None."""
    path = north_home / _PROGRESS_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("bootstrap: ignoring corrupt progress file %s", path)
        return None
    if not isinstance(data, list):
        return None
    if data and isinstance(data[0], str):
        return [{"path": p, "hash": "", "mtime": 0.0, "status": "completed"} for p in data]
    return data


def _save_progress(north_home: Path, completed: list[dict]) -> None:
    """Atomically checkpoint the set of completed file paths with hashes."""
    path = north_home / _PROGRESS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(completed), encoding="utf-8")
    tmp.replace(path)


_MAX_FILE_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 3.0
_MAX_RETRY_BACKOFF_SECONDS = 20.0
_NAMED_IN_LOG = 5


class _Checkpoint:
    """The completed-file record that lets a paused bootstrap run resume."""

    def __init__(self, north_home: Path, completed: set[str]) -> None:
        self._north_home = north_home
        self.completed = completed
        # Hash and stat each file once. The snapshot is rebuilt after every file,
        # and hashing reads the whole file, so recomputing per snapshot made this
        # O(files²) reads for no benefit.
        self._fingerprints: dict[str, tuple[str, float]] = {}

    def mark_done(self, path: Path) -> None:
        self.completed.add(str(path.resolve()))

    def is_done(self, path: Path) -> bool:
        return str(path.resolve()) in self.completed

    async def save(self, files: list[Path]) -> None:
        """Persist the completed-file checkpoint, off the event loop."""
        items = []
        for path in files:
            resolved = str(path.resolve())
            if resolved not in self.completed:
                continue
            if resolved not in self._fingerprints:
                self._fingerprints[resolved] = await asyncio.to_thread(_fingerprint, path)
            file_hash, mtime = self._fingerprints[resolved]
            items.append({"path": resolved, "hash": file_hash, "mtime": mtime, "status": "completed"})
        await asyncio.to_thread(_save_progress, self._north_home, items)


async def _extract_with_retries(
    path: Path,
    inference_router: InferenceRouter,
    content: str | None,
) -> tuple[list[dict], str] | None:
    """Facts and profile text for one file, or None when no model would answer.

    A model that is merely rate-limited is worth waiting for; anything else is a
    file north cannot read, which is an empty result rather than a paused run.
    """
    for attempt in range(_MAX_FILE_RETRIES):
        try:
            return await _extract_unified(path, inference_router, content=content)
        except AllModelsRateLimitedError as e:
            logger.warning(
                "bootstrap: rate limited extracting %s (attempt %d/%d): %s",
                path,
                attempt + 1,
                _MAX_FILE_RETRIES,
                e,
            )
            await asyncio.sleep(min(_RETRY_BACKOFF_SECONDS * (2**attempt), _MAX_RETRY_BACKOFF_SECONDS))
        except Exception:
            logger.warning("bootstrap: failed to extract from %s", path, exc_info=True)
            return [], ""
    return None


async def _store_extracted_facts(fact_store: FactStore, path: Path, candidates: list[dict]) -> int:
    """Write one file's facts, returning how many were kept."""
    stored = 0
    for candidate in candidates:
        try:
            if await fact_store.add_fact_with_provenance(
                content=candidate["content"],
                category=candidate.get("topic") or FactTopic.OTHER.value,
                subject=candidate["subject"],
                confidence=candidate["confidence"],
                status="active",
                source_path=candidate["source_path"],
                source_hash=candidate["source_hash"],
                source_mtime=candidate["source_mtime"],
                evidence=candidate["evidence"],
            ):
                stored += 1
        except Exception:
            logger.warning("bootstrap: failed to store fact from %s", path, exc_info=True)
    return stored


async def _write_user_profile(context_store: ContextStore | None, sections: list[str]) -> None:
    """Keep the fact store atomic - each durable fact stays a single claim - and
    write the readable profile to the USER context document. This was extracted
    on every file and then dropped on the floor, which is why the whole-document
    fallback in the memory gateway had nothing to fall back to.
    """
    if context_store is None or not sections:
        return
    synthesized = _synthesize_profile(sections)
    if not synthesized:
        return
    try:
        await context_store.write(ContextDocument.USER, synthesized)
        logger.info("bootstrap: wrote a user profile from %d file(s)", len(sections))
    except Exception:
        logger.warning("bootstrap: could not write the user profile document", exc_info=True)


async def _mark_bootstrap_complete(north_home: Path) -> None:
    """Write the versioned marker; only ever called on a clean completion."""
    await asyncio.to_thread(
        (north_home / _BOOTSTRAPPED_MARKER).write_text,
        json.dumps({"bootstrap_version": _BOOTSTRAP_VERSION, "completed_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )
    await asyncio.to_thread((north_home / _PROGRESS_FILE).unlink, missing_ok=True)


def _report_bootstrap_outcome(outcomes: Counter[str], total_facts: int, file_count: int) -> None:
    productive = outcomes["yielded facts"]
    logger.info(
        "bootstrap: done — stored %d facts from %d of %d files (%d yielded nothing)",
        total_facts,
        productive,
        file_count,
        outcomes["yielded nothing"],
    )
    if productive < file_count / 2:
        # More than half the run produced nothing. That is a routing or extraction
        # problem, not a property of the files, and it should be visible without
        # anyone going through the log line by line.
        logger.warning(
            "bootstrap: only %d of %d files produced facts - check that a model honouring "
            "the response schema was available",
            productive,
            file_count,
        )


async def _files_worth_reading(checkpoint: _Checkpoint, selected_paths: set[str] | None) -> list[SurveyedFile]:
    """The files this run should extract from; empty when there is nothing to do."""
    candidates = _discover_files()
    if selected_paths:
        candidates = [path for path in candidates if str(path.resolve()) in selected_paths]
    if not candidates:
        logger.info("bootstrap: no candidate files found — marking done")
        return []

    unprocessed = [path for path in candidates if not checkpoint.is_done(path)]
    if not unprocessed:
        logger.info("bootstrap: all %d files already processed — marking done", len(candidates))
        return []

    # Read the candidates locally and keep only those worth a model call. Doing
    # this first is what stops the run spending its budget on three copies of one
    # transcript, a PDF with no text layer, and somebody else's resume.
    survey = await asyncio.to_thread(
        survey_files,
        unprocessed,
        read_text=_read_text,
        user_tokens=_get_user_tokens(),
        budget=_MAX_FILES,
    )
    logger.info("bootstrap: surveyed %d candidates — %s", len(unprocessed), survey.summary())
    for path, reason in survey.dropped:
        logger.debug("bootstrap: skipping %s (%s)", path.name, reason)
    if not survey.kept:
        logger.info("bootstrap: no file survived the survey — marking done")
    return survey.kept


async def _bootstrap_already_done(
    fact_store: FactStore,
    progress: list[dict] | None,
    selected_paths: set[str] | None,
) -> bool:
    """True when the store already holds bootstrapped facts and no run is pending.

    Facts are filed under their topic now, so "has bootstrap run?" spans the
    whole topic set. It must stay narrower than a plain total: facts learned in
    conversation are filed under their context document, and those must never
    block a first-ever bootstrap.
    """
    if progress is not None or selected_paths:
        return False
    existing = await fact_store.count_in_categories(_VALID_TOPICS)
    if existing <= 0:
        return False
    logger.debug("bootstrap: skipped (fact store already has %d facts)", existing)
    return True


async def run_bootstrap_if_needed(
    fact_store: FactStore | None,
    inference_router: InferenceRouter,
    north_home: Path,
    selected_paths: set[str] | None = None,
    context_store: ContextStore | None = None,
) -> None:
    """Seed facts from local files on first-ever start.

    Checks for {north_home}/.bootstrapped — if present, returns immediately.
    Otherwise discovers high-ROI files, extracts facts via unified LLM call,
    and stores them in the fact store.

    Safe pause-and-resume: If models are unavailable or rate-limited, the run
    pauses with checkpoint saved, and does NOT write the completion marker.
    """
    if fact_store is None:
        logger.info("bootstrap: skipped (no fact store)")
        return

    marker = north_home / _BOOTSTRAPPED_MARKER
    if marker.exists() and not selected_paths:
        logger.debug("bootstrap: already seeded (marker found)")
        return

    progress = _load_progress(north_home)
    if await _bootstrap_already_done(fact_store, progress, selected_paths):
        marker.touch()
        return

    checkpoint = _Checkpoint(
        north_home,
        {item["path"] for item in (progress or []) if item.get("status") == "completed"},
    )
    surveyed = await _files_worth_reading(checkpoint, selected_paths)
    if not surveyed:
        marker.touch()
        return

    files = [item.path for item in surveyed]
    text_by_path = {item.path: item.text for item in surveyed}
    logger.info(
        "bootstrap: extracting from %d files (%d already done): %s",
        len(files),
        len(checkpoint.completed),
        ", ".join(p.name for p in files[:_NAMED_IN_LOG]) + ("…" if len(files) > _NAMED_IN_LOG else ""),
    )

    outcomes: Counter[str] = Counter()
    total_facts = 0
    profile_sections: list[str] = []
    for path in files:
        extracted = await _extract_with_retries(path, inference_router, text_by_path.get(path))
        if extracted is None:
            # Model access unavailable during all retries: safely PAUSE the run -
            # save the checkpoint and do NOT write the completion marker.
            logger.warning(
                "bootstrap: pausing bootstrap run because models are unavailable for %s (%d files remain pending)",
                path,
                len(files) - len(checkpoint.completed),
            )
            await checkpoint.save(files)
            return

        candidates, profile_md = extracted
        stored_here = await _store_extracted_facts(fact_store, path, candidates)
        total_facts += stored_here
        # A file that yields nothing is a failure worth naming. Silently counting
        # it as processed is how a run that extracted one fact from twenty-five
        # files reported itself as done.
        outcomes["yielded facts" if stored_here else "yielded nothing"] += 1
        if not stored_here:
            logger.warning("bootstrap: %s produced no usable facts", path.name)
        if profile_md:
            profile_sections.append(profile_md)

        checkpoint.mark_done(path)
        await checkpoint.save(files)
        await asyncio.sleep(_BOOTSTRAP_DELAY_SECONDS)

    await _write_user_profile(context_store, profile_sections)
    await _mark_bootstrap_complete(north_home)
    _report_bootstrap_outcome(outcomes, total_facts, len(files))
