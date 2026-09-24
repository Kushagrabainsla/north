"""The "north." wordmark: white "north", a green "." - matching the web
dashboard's brand mark (web/src/components.tsx .brand-wordmark) so the CLI,
TUI, and dashboard read as one product."""

from __future__ import annotations

from rich.console import Console

from cli.constants import WORDMARK_ACCENT
from cli.formatting import wordmark


def test_wordmark_is_bold_white_name_plus_accent_dot():
    assert wordmark() == f"[bold white]north[/bold white][bold {WORDMARK_ACCENT}].[/bold {WORDMARK_ACCENT}]"


def test_dim_wordmark_dims_the_name_but_not_the_dot():
    """A panel title is quieter than a heading, but the dot is the mark - it
    never fades just because the text around it does."""
    dimmed = wordmark(dim=True)
    assert "[dim]north[/dim]" in dimmed
    assert WORDMARK_ACCENT in dimmed


def test_wordmark_renders_to_real_ansi_colour():
    """Confirms the markup is valid Rich syntax that actually paints the dot
    in the dashboard's green, not just a string that happens to look right."""
    console = Console(force_terminal=True, color_system="truecolor")
    with console.capture() as capture:
        console.print(wordmark(), end="")
    rendered = capture.get()
    assert "north" in rendered
    assert "184;239;114" in rendered  # #b8ef72 as truecolor RGB
