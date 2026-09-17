"""Utility to load markdown prompt templates.

See docs/CODING_STYLE.md Section 5.3.
"""

from __future__ import annotations

from pathlib import Path

from utils.runtime_resources import resource_path


def load_prompt(path: str | Path) -> str:
    """Load a markdown prompt file.

    Args:
        path: Path to the markdown prompt file.

    Returns:
        The content of the prompt file as a string.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    p = Path(path)
    if not p.is_absolute():
        # Preserve the public ``prompts/foo.md`` spelling while resolving the
        # file from the packaged ``resources/prompts`` directory.
        relative = p.relative_to("prompts") if p.parts and p.parts[0] == "prompts" else p
        p = resource_path(Path("prompts") / relative)
    if not p.exists():
        raise FileNotFoundError(f"Prompt file not found at: {p.absolute()}")
    return p.read_text(encoding="utf-8").strip()
