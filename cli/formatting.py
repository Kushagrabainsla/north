"""Pure presentation helpers for the north CLI and TUI.

Formatting and rendering functions with no I/O and no shared state - they take
values and return strings or Rich renderables. Kept out of `cli/main.py` and
`cli/tui.py` so those files hold commands and the App, not display plumbing (§4.1).
"""

from __future__ import annotations

import re

from rich.table import Table
from rich.text import Text

from cli.constants import _FILL_COLOURS, _MARKUP_RE, _SLASH_COMMANDS


def _reconstruct_task_output(entries: list[dict]) -> str:
    """Join the ``agent_completed`` outputs from ledger *entries* into one string."""
    return "\n\n".join(
        e["output"] for e in entries if e.get("action") == "agent_completed" and e.get("output")
    )


def _fmt_params(params: dict) -> str:
    parts = []
    for k, v in params.items():
        if k in ("workspace", "task_id"):
            continue
        v_str = repr(v)
        if len(v_str) > 60:
            v_str = v_str[:57] + "…'"
        parts.append(f"{k}={v_str}")
    return ", ".join(parts[:4])


def _short_model(model: str) -> str:
    """Trim a router model id (e.g. 'meta-llama/llama-4-scout-17b:free') to a
    compact label for the info bar."""
    name = model.rsplit("/", 1)[-1].removesuffix(":free")
    return name if len(name) <= 28 else name[:27] + "…"


def _compute_suggestion(value: str, history: list[str]) -> str | None:
    """Ghost-text completion for the prompt: slash commands when the line starts
    with '/', otherwise the most recent matching history entry."""
    if not value:
        return None
    if value.startswith("/"):
        for cmd in _SLASH_COMMANDS:
            if cmd.startswith(value) and cmd != value:
                return cmd
        return None
    for past in reversed(history):
        if past.startswith(value) and past != value:
            return past
    return None


def _strip_markup(s: str) -> str:
    """Remove Textual console-markup tags so a segment's display width can be
    measured. Only well-formed [tag] / [/tag] spans are removed."""
    return _MARKUP_RE.sub("", s)


def _to_prose(md: str) -> str:
    """Flatten markdown to clean terminal prose: drop ``` fences, heading hashes,
    and bold/italic/inline-code markers, while preserving code-block *content*
    and list structure verbatim.

    NOTE: not for the chat view - assistant messages render through a real
    markdown engine (RichMarkdown / the streaming Markdown widget) so tables and
    lists survive. This flattener has no table support and is kept only for
    plain-text sinks (exports, logs) where there is no renderer to defer to."""
    out: list[str] = []
    in_code = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code  # drop the fence marker line itself
            continue
        if in_code:
            out.append(line)  # preserve code lines exactly
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)  # heading hashes
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # **bold**
        line = re.sub(r"__(.+?)__", r"\1", line)  # __bold__
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)  # *italic*
        line = re.sub(r"`([^`]+)`", r"\1", line)  # `inline code`
        out.append(line)
    return "\n".join(out)


def _fmt_tokens(n: int) -> str:
    """Compact token count: 940 → '940', 12_400 → '12.4K', 200_000 → '200K'."""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.0f}K" if k >= 100 or k == int(k) else f"{k:.1f}K"


def _fmt_elapsed(seconds: float) -> str:
    """Elapsed session time: '0:42', '12:05', '1:03:20'."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fill_bar(fraction: float, width: int = 10) -> str:
    """Block-character fill bar coloured green→yellow→orange→red by fraction."""
    fraction = max(0.0, min(1.0, fraction))
    colour = next(c for limit, c in _FILL_COLOURS if fraction < limit)
    filled = int(round(fraction * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{colour}]{bar}[/{colour}]"


def _build_steps_table(steps: list[tuple[str, str, bool]]) -> Table:
    """Render pipeline steps as a borderless table. Each step is (icon, label, active)."""
    t = Table.grid(padding=(0, 2))
    t.add_column(width=1)
    t.add_column()
    for icon, label, active in steps:
        if active:
            t.add_row(
                Text(icon, style="white"),
                Text(label, style="white"),
            )
        else:
            t.add_row(
                Text(icon, style="dim green"),
                Text(label, style="dim"),
            )
    return t
