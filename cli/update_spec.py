"""Pure install-specification helpers for the CLI update command."""

from __future__ import annotations


def pinned_git_spec(install_url: str) -> str:
    """Return the install URL as a uv git spec, pinning to main when unpinned."""
    spec = f"git+{install_url}"
    if spec.endswith("@main") or "@" in spec.split("/")[-1]:
        return spec
    return f"{spec}@main"
