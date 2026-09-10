"""Frontend asset freshness checks for CLI startup."""

from __future__ import annotations

from pathlib import Path


def web_build_is_stale(web_dir: Path) -> bool:
    """Return whether bundled assets are absent or older than frontend inputs."""
    dist_index = web_dir / "dist" / "index.html"
    if not dist_index.is_file():
        return True
    input_names = ("package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "index.html")
    inputs = [web_dir / name for name in input_names]
    src_dir = web_dir / "src"
    if src_dir.is_dir():
        inputs.extend(path for path in src_dir.rglob("*") if path.is_file())
    input_mtime = max((path.stat().st_mtime for path in inputs if path.is_file()), default=0)
    return dist_index.stat().st_mtime < input_mtime
