"""Platform-owned filesystem traversal constants.

These are pure data with no dependency on the tool layer: the set of directory
names never worth walking for a coding task. Owning them here (platform) lets
non-tool callers share the exclusion list without importing ``tools._path``.

``tools._path`` re-exports :data:`PRUNED_DIRS` so existing tool importers (and
their test seams) keep working unchanged. See docs/CODING_STYLE.md Section 16.1.1.
"""

from __future__ import annotations

# The caches a run of a tool leaves behind: Python's bytecode, and test and lint caches. Never anyone's source.
CACHE_DIRS: frozenset[str] = frozenset({"__pycache__", ".ruff_cache", ".pytest_cache"})

# Directories never worth walking for a coding task. Shared by the file-walking
# tools (search_files, glob) so the exclusion list cannot drift.
PRUNED_DIRS: frozenset[str] = CACHE_DIRS | {".git", "node_modules", ".venv", "venv", "build", "dist"}
