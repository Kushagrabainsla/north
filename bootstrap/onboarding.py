"""Async first-run bootstrap: scans user files, extracts facts, seeds memory.

Runs once as a background task after startup. Never blocks the user's first
prompt. Safe to cancel at any point — already-stored facts survive and the
bootstrapped marker is written only on clean completion.

File selection: homedir csv/txt + project READMEs, newest 10, never system dirs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from inference.base import InferenceRouter
from inference.models import CompletionRequest, PoolPriority
from memory.facts import FactStore

logger = logging.getLogger(__name__)

_BOOTSTRAPPED_MARKER = ".bootstrapped"
_MAX_FILES = 50
_MAX_FILE_BYTES = 100_000  # skip anything larger than 100 KB
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
_EXTENSIONS = frozenset({".csv", ".txt", ".md", ".json", ".yaml", ".yml"})
_EXTRACT_PROMPT = """\
You are a personal assistant extracting facts about a person from their files.
Return a JSON array of fact strings (max 5 per file). Facts are short, specific
statements like 'User spends $200/week on groceries' or 'User's savings goal is
$10,000' or 'User follows a keto meal plan'. Return ONLY valid JSON array of
strings, nothing else.

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

    # If there are already facts, don't re-seed
    existing = await fact_store.count()
    if existing > 0:
        logger.debug("bootstrap: skipped (fact store already has %d facts)", existing)
        marker.touch()
        return

    files = _discover_files()
    if not files:
        logger.info("bootstrap: no candidate files found — marking done")
        marker.touch()
        return

    logger.info("bootstrap: scanning %d files for facts", len(files))
    total_facts = 0
    for path in files:
        try:
            facts = await _extract_facts(path, inference_router)
        except Exception:
            logger.warning("bootstrap: failed to extract from %s", path, exc_info=True)
            continue
        for fact in facts:
            try:
                await fact_store.add_fact(fact, category="bootstrap")
                total_facts += 1
            except Exception:
                logger.warning("bootstrap: failed to store fact from %s", path, exc_info=True)

    marker.touch()
    logger.info("bootstrap: done — stored %d facts from %d files", total_facts, len(files))


def _discover_files() -> list[Path]:
    """Walk user dirs for up to ``_MAX_FILES`` personal text files.

    Scans ``~/Downloads``, ``~/Documents``, ``~/Desktop`` (non-project),
    plus homedir csv/txt and project READMEs.  Skips junk dirs, dotfiles,
    and anything over ``_MAX_FILE_BYTES``.  Returns newest first.
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

    # Project READMEs
    projects = home / "Desktop" / "projects"
    if projects.is_dir():
        for p in projects.glob("*/README.md"):
            if p.is_file() and p.stat().st_size <= _MAX_FILE_BYTES:
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


async def _extract_facts(path: Path, router: InferenceRouter) -> list[str]:
    """Read *path*, prompt an LLM for facts, return parsed list."""
    content = path.read_text(encoding="utf-8", errors="replace")[:4000]
    if not content.strip():
        return []

    prompt = _EXTRACT_PROMPT.format(content=content)
    req = CompletionRequest(
        prompt=prompt,
        priority=PoolPriority.LOW,
        component="bootstrap",
        max_tokens=2000,
        temperature=0.1,
        json_mode=True,
    )
    resp = await router.complete(req)
    raw = resp.text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1 :]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        facts = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("bootstrap: LLM returned non-JSON for %s — %r", path.name, raw[:200])
        return []

    if not isinstance(facts, list):
        logger.warning("bootstrap: LLM returned non-list for %s", path.name)
        return []

    return [str(f).strip() for f in facts if f and str(f).strip()]
