"""Compact repository map for grounding engineering agents (#2).

Gives an agent an up-front, bird's-eye view of the codebase - the key files and
the top-level classes/functions each defines - so it doesn't have to rediscover
the whole tree via tools on every task. Inspired by Aider's repo map: a concise,
budget-bounded map of files and their key symbols.

Deliberately lightweight: top-level symbols only, ranked by shallowness and
entry-point hints, capped by a character budget. It is a navigation aid, not a
semantic index.
"""

from __future__ import annotations

import ast
import os
import re
import time
from pathlib import Path

from tools._path import PRUNED_DIRS, is_sensitive_path

_CODE_EXTS: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h", ".hpp", ".kt", ".swift"}
)
_ENTRYPOINT_HINTS: tuple[str, ...] = ("main", "index", "app", "__init__", "cli", "server", "core", "api")
_MAX_SYMBOLS_PER_FILE: int = 12
_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:public\s+|pub\s+)?"
    r"(class|function|func|interface|type|def)\s+([A-Za-z_]\w*)"
)

_REPO_MAP_CACHE: dict[tuple[str, int, int], tuple[float, str]] = {}
_REPO_MAP_CACHE_TTL: float = 60.0  # seconds


def clear_repo_map_cache() -> None:
    """Clear in-memory repo map cache."""
    _REPO_MAP_CACHE.clear()


def build_repo_map(workspace: str, *, max_files: int = 40, max_chars: int = 4000) -> str:
    """Return a compact map of *workspace* (files + key symbols), within budgets."""
    if not workspace:
        return ""
    now = time.monotonic()
    cache_key = (workspace, max_files, max_chars)
    if cache_key in _REPO_MAP_CACHE:
        cached_time, cached_val = _REPO_MAP_CACHE[cache_key]
        if now - cached_time < _REPO_MAP_CACHE_TTL:
            return cached_val

    root = Path(workspace)
    if not root.is_dir():
        return ""

    blocks: list[str] = []
    used = 0
    for rel, path in _collect_source_files(root, max_files):
        symbols = _top_symbols(path)
        if not symbols:
            continue
        listed = symbols[:_MAX_SYMBOLS_PER_FILE]
        block = f"{rel}\n" + "\n".join(f"  {s}" for s in listed)
        if used + len(block) + 1 > max_chars:
            break
        blocks.append(block)
        used += len(block) + 1
    result = "\n".join(blocks)
    _REPO_MAP_CACHE[cache_key] = (now, result)
    return result


def _collect_source_files(root: Path, max_files: int) -> list[tuple[str, Path]]:
    """Return up to *max_files* source files, shallowest and entry-point-like first."""
    candidates: list[tuple[int, int, str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _CODE_EXTS or is_sensitive_path(path):
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            depth = len(rel.parts)
            is_entry = 0 if any(h in filename.lower() for h in _ENTRYPOINT_HINTS) else 1
            candidates.append((depth, is_entry, str(rel), path))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return [(rel, path) for _, _, rel, path in candidates[:max_files]]


def _top_symbols(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix.lower() == ".py":
        return _python_top_symbols(text)
    return _regex_top_symbols(text)


def _python_top_symbols(text: str) -> list[str]:
    """Top-level classes and functions with argument names, via AST (no execution)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(f"class {node.name}")
        elif isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            args = ", ".join(a.arg for a in node.args.args)
            out.append(f"{prefix} {node.name}({args})")
    return out


def _regex_top_symbols(text: str) -> list[str]:
    """Best-effort top-level declarations for non-Python languages (heuristic)."""
    out: list[str] = []
    for line in text.splitlines():
        if _DECL_RE.match(line):
            out.append(line.strip()[:120])
    return out
