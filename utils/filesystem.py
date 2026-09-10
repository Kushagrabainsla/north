"""Platform-owned filesystem traversal constants.

These are pure data with no dependency on the tool layer: the set of directory
names never worth walking for a coding task. Owning them here (platform) lets
non-tool callers - e.g. the orchestrator's commit path, which reads what changed
on disk - share the exclusion list without importing ``tools._path``.

``tools._path`` re-exports :data:`PRUNED_DIRS` so existing tool importers (and
their test seams) keep working unchanged, and so the file-walking tools and the
commit path can never drift apart. See docs/CODING_STYLE.md Section 16.1.1.
"""

from __future__ import annotations

# Directories never worth walking for a coding task. Shared by the file-walking
# tools (search_files, glob) and the orchestrator commit path so the exclusion
# list cannot drift.
PRUNED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache", "build", "dist"}
)
