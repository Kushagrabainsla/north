"""Async first-run bootstrap: scans user files, extracts facts, seeds memory.

Runs once as a background task after startup. Never blocks the user's first
prompt. Safe to cancel at any point — already-stored facts survive and the
bootstrapped marker is written only on clean completion.

File selection: Downloads/Documents/Desktop (recursive, up to 200 files),
homedir csv/txt, project READMEs — newest first, never system dirs, never
north's own checkout.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from bootstrap.schema import EXTRACTED_FACTS_JSON_SCHEMA
from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from memory.facts import FactStore

logger = logging.getLogger(__name__)

_BOOTSTRAPPED_MARKER = ".bootstrapped"
_PROGRESS_FILE = ".bootstrap_progress.json"
_MAX_FILES = 200
_MAX_FILE_BYTES = 500_000  # skip anything larger than 500 KB
_SOURCE_DIRS = ("Downloads", "Documents", "Desktop")
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
_EXTRACT_PROMPT = """\
You are a personal assistant extracting facts about a person from their files.
Return a JSON array of fact strings (max 5 per file). Facts are short, specific
statements like 'User spends $200/week on groceries' or 'User's savings goal is
$10,000' or 'User follows a keto meal plan'.

ONLY extract facts about the person themselves: their habits, finances, health,
schedule, preferences, projects, and background. SKIP content that describes AI
systems, chatbots, or assistants (system prompts, model capabilities, tool
documentation, conversation logs, data exports) — those are not facts about the
person. SKIP roleplay scenarios, simulated conversations, and fictional
personas. SKIP generic environment details (OS, shell, working directory).
Files about OTHER people (tax documents, legal forms, letters addressed to
someone else) describe third parties, not the user — SKIP them. Only extract
facts where the person described is clearly the user.

Only extract facts that are EXPLICITLY STATED in the file content or strongly
implied by it. NEVER invent, guess, or fabricate facts (no made-up names,
birthdays, pets, preferences, or favorite colors). Never invent specific
details — dates, times, amounts, IDs, numbers, colors, or names — that are
not literally present in the file.

Never emit negative or absence statements ("X is not mentioned", "no
information available", "the user has not stated..."). Those are
meta-commentary, not facts — if the file yields nothing real, return [].

Never emit identification numbers (I-94, passport, SSN, visa, or account
numbers), even when present in a file — they are sensitive, not useful facts.

Never emit generic filler ("user works on coding tasks", "user is involved
in a team project"). Every fact must be a specific, real detail about the
user that you read in the file.

Return ONLY valid JSON array of strings, nothing else.

File content:
---
{content}
---
"""


async def run_bootstrap_if_needed(
    fact_store: FactStore | None,
    inference_router: InferenceRouter,
    north_home: Path,
) -> None:
    """Seed facts from local files on first-ever start.

    Checks for ``{north_home}/.bootstrapped`` — if present, returns
    immediately. Otherwise discovers files, extracts facts via LLM, and
    stores them in the fact store. Writes the marker only after all
    extraction completes successfully.

    Progress is checkpointed to ``{north_home}/.bootstrap_progress.json``
    after every file, so an interrupted run (server restart, crash) resumes
    from where it stopped instead of re-extracting everything. The progress
    file is deleted on clean completion.

    Errors are logged and swallowed per-file so one bad file doesn't
    kill the whole bootstrap.
    """
    if fact_store is None:
        logger.info("bootstrap: skipped (no fact store)")
        return

    marker = north_home / _BOOTSTRAPPED_MARKER
    if marker.exists():
        logger.debug("bootstrap: already seeded (marker found)")
        return

    progress = _load_progress(north_home)

    # Facts exist but no run is in flight — the store is already seeded
    # (e.g. the marker was deleted after a completed run). Never re-seed.
    # Only bootstrap-category facts count here: unrelated user-learned
    # facts must not block a first-ever bootstrap.
    existing = await fact_store.count(category="bootstrap")
    if existing > 0 and progress is None:
        logger.debug("bootstrap: skipped (fact store already has %d facts)", existing)
        marker.touch()
        return

    files = _discover_files()
    if not files:
        logger.info("bootstrap: no candidate files found — marking done")
        marker.touch()
        return

    completed = set(progress or ())
    pending = [p for p in files if str(p.resolve()) not in completed]
    if not pending:
        logger.info("bootstrap: all %d files already processed — marking done", len(files))
        marker.touch()
        return

    logger.info(
        "bootstrap: scanning %d files for facts (%d new, %d already done)",
        len(files),
        len(pending),
        len(completed),
    )
    total_facts = 0
    for path in pending:
        try:
            facts = await _extract_facts(path, inference_router)
        except Exception:
            logger.warning("bootstrap: failed to extract from %s", path, exc_info=True)
            continue
        for fact in facts:
            try:
                # Dedup lives in the store (exact match + cosine), exactly like
                # every other fact writer — bootstrap adds no second mechanism.
                if await fact_store.add_fact(fact, category="bootstrap"):
                    total_facts += 1
            except Exception:
                logger.warning("bootstrap: failed to store fact from %s", path, exc_info=True)
        completed.add(str(path.resolve()))
        _save_progress(north_home, sorted(completed))

    marker.touch()
    (north_home / _PROGRESS_FILE).unlink(missing_ok=True)
    logger.info("bootstrap: done — stored %d facts from %d files", total_facts, len(pending))


def _is_projects_dir(path: Path, home: Path) -> bool:
    """True if *path* is the user's ``~/Desktop/projects`` directory.

    Project repos are code, not personal facts: recursing into them pulls in
    AI-system docs (e.g. ``system_prompts_leaks``), skill files, and build
    artifacts that the extractor mistakes for facts about the user. Only the
    explicit README.md pass (user-authored project descriptions) contributes
    from projects.
    """
    projects = (home / "Desktop" / "projects").resolve()
    return path.resolve() == projects


def _is_under_north_repo(path: Path, home: Path) -> bool:
    """True if *path* is inside north's own checkout.

    The repo's README, skills, and docs describe the assistant itself —
    extracting them as "user facts" pollutes the fact store with
    self-descriptions like "User is named north".
    """
    repo = (home / "Desktop" / "projects" / "north").resolve()
    resolved = path.resolve()
    return resolved == repo or repo in resolved.parents


def _discover_files() -> list[Path]:
    """Walk user dirs for up to ``_MAX_FILES`` personal text files.

    Scans ``~/Downloads``, ``~/Documents``, ``~/Desktop`` (non-project),
    plus homedir csv/txt and project READMEs.  Skips junk dirs, dotfiles,
    north's own checkout, and anything over ``_MAX_FILE_BYTES``.  Returns
    newest first.
    """
    home = Path.home()
    candidates: list[Path] = []

    def _walk(root: Path) -> None:
        """Recursively walk *root*, collecting matching files."""
        if not root.is_dir():
            return
        try:
            for entry in root.iterdir():
                name = entry.name
                if name.startswith(".") or name in _SKIP_DIRS:
                    continue
                if _is_under_north_repo(entry, home):
                    continue
                if _is_projects_dir(entry, home):
                    # Repos are code, not personal facts. Project READMEs are
                    # collected separately (user-authored descriptions).
                    continue
                if entry.is_dir():
                    _walk(entry)
                elif entry.is_file() and entry.suffix.lower() in _EXTENSIONS:
                    if entry.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    candidates.append(entry)
        except PermissionError:
            pass

    # Source directories: Downloads, Documents, Desktop (skip projects/)
    for rel in _SOURCE_DIRS:
        d = home / rel
        if d.is_dir():
            _walk(d)

    # Homedir: flat csv/txt only (not recursive — avoids .config, .ssh etc.)
    for p in home.glob("*.csv"):
        if p.is_file() and p.stat().st_size <= _MAX_FILE_BYTES:
            candidates.append(p)
    for p in home.glob("*.txt"):
        if p.is_file() and p.stat().st_size <= _MAX_FILE_BYTES:
            candidates.append(p)

    # Project READMEs (north's own repo excluded — see _is_under_north_repo)
    projects = home / "Desktop" / "projects"
    if projects.is_dir():
        for p in projects.glob("*/README.md"):
            if p.is_file() and p.stat().st_size <= _MAX_FILE_BYTES and not _is_under_north_repo(p, home):
                candidates.append(p)

    # Dedup and sort newest-first
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in candidates:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(p)
    deduped.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return deduped[:_MAX_FILES]


def _read_text(path: Path) -> str:
    """Extract plain text from a file — supports plain text, PDF, and DOCX.

    Falls back to empty string when the required library is not installed
    (gracefully skips the file instead of crashing).
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
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            logger.warning("bootstrap: failed to extract text from PDF %s", path.name, exc_info=True)
            return ""

    if suffix in (".docx",):
        try:
            from docx import Document
        except ImportError:
            logger.warning("bootstrap: python-docx not installed — skipping %s", path.name)
            return ""
        try:
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            logger.warning("bootstrap: failed to extract text from DOCX %s", path.name, exc_info=True)
            return ""

    # Plain text
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.warning("bootstrap: failed to read %s", path.name, exc_info=True)
        return ""


async def _extract_facts(path: Path, router: InferenceRouter) -> list[str]:
    """Read *path* (text, PDF, or DOCX), prompt an LLM for facts, return list."""
    content = _read_text(path)[:4000]
    if not content.strip():
        return []

    prompt = _EXTRACT_PROMPT.format(content=content)
    req = CompletionRequest(
        prompt=prompt,
        priority=PoolPriority.LOW,
        component="bootstrap",
        max_tokens=2000,
        temperature=0.1,
        response_schema=EXTRACTED_FACTS_JSON_SCHEMA,
    )
    resp = await router.complete(req)
    raw = resp.text.strip()

    # Structured output guarantees valid JSON matching the schema
    try:
        extracted = json.loads(raw)
        facts = [item["content"] for item in extracted.get("facts", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("bootstrap: LLM returned invalid structured output for %s — %r", path.name, raw[:200])
        return []

    cleaned: list[str] = []
    for fact in facts:
        text = _clean_fact(fact)
        if text is not None:
            cleaned.append(text)
    return cleaned


def _clean_fact(fact: object) -> str | None:
    """Normalize one LLM extraction result into a plain fact string.

    Some models return objects like ``{"fact": "...", "source": "..."}``
    under json_mode — take the ``fact`` (or ``content``/``text``) field
    instead of stringifying the whole dict. Returns None for anything
    that isn't a usable fact string, and for extraction noise the model
    is prone to: absence statements, identification numbers, and facts
    that don't reference the user at all.
    """
    if isinstance(fact, dict):
        fact = fact.get("fact") or fact.get("content") or fact.get("text")
    if fact is None:
        return None
    text = str(fact).strip()
    if not text or not _is_usable_fact(text):
        return None
    return text


# Absence/negative statements ("X is not mentioned", "no information
# available") are meta-commentary, not facts about the user.
_ABSENCE_RE = re.compile(
    r"\b(?:not|no|never|nothing|n't)\b.{0,30}"
    r"\b(?:mention|state|provid|availab|inform|explicit|discuss|record|found|known|indicat|list)\w*",
    re.IGNORECASE,
)

# Identification numbers (I-94, passport, SSN, visa, account) are sensitive
# and useless as recall facts — drop them even if the model extracted one.
_PII_RE = re.compile(
    r"\b(?:i-?94|passport|ssn|social security|visa number|account number|admission record number)\b",
    re.IGNORECASE,
)

# A stored fact must reference the user (by name, pronoun, or document
# role). Facts about third parties, orgs, or generic content ("The I-9
# Service Center handled the receipt", "Real Tucson resources: ...")
# describe someone else — drop them.
_USER_REF_RE = re.compile(
    r"\b(?:user|kushagra|bainsla|he|his|him|she|her|their|they|employee)\b",
    re.IGNORECASE,
)


def _is_usable_fact(text: str) -> bool:
    return not (_ABSENCE_RE.search(text) or _PII_RE.search(text) or not _USER_REF_RE.search(text))


def _load_progress(north_home: Path) -> list[str] | None:
    """Return completed file paths from the progress checkpoint, or None.

    None means "no run in flight" (fresh install, or marker deleted after a
    completed run); an empty list means a run was checkpointed with nothing
    done yet. A corrupt checkpoint (crash mid-write) is treated as None.
    """
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
    return [str(p) for p in data]


def _save_progress(north_home: Path, completed: list[str]) -> None:
    """Atomically checkpoint the set of completed file paths."""
    path = north_home / _PROGRESS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(completed), encoding="utf-8")
    tmp.replace(path)
